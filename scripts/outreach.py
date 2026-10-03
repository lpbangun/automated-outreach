#!/usr/bin/env python3
"""Standalone host-assisted outreach pipeline. No sending surface."""
import argparse
import contextlib
import datetime as dt
from decimal import Decimal, ROUND_CEILING
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
import uuid

MODEL = 'typesafe/jev-1.13'
API = 'https://openrouter.ai/api/alpha/decisions'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def micro(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError('Cost must be finite and nonnegative')
    return int((amount * 1000000).to_integral_value(rounding=ROUND_CEILING))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Budget:
    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def locked(self):
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            doc = read(self.path)
            yield doc
            write(self.path, doc)

    def reserve(self, provider, call_id, maximum, fingerprint=None):
        maximum = micro(maximum)
        if maximum <= 0:
            raise ValueError('Reserve a positive maximum, including nominally free routes')
        with self.locked() as doc:
            if any(row.get('over_reservation') for row in doc['calls'].values()):
                raise ValueError('Prior provider overcharge; ledger frozen pending audit')
            if call_id in doc['calls']:
                raise ValueError('Existing dispatch ID; reconcile or use saved response, never redispatch')
            if provider not in doc['caps_micro']:
                raise ValueError('Unknown provider')
            used = sum(row.get('actual_micro', row['reserved_micro'])
                       for row in doc['calls'].values() if row['provider'] == provider)
            if used + maximum > doc['caps_micro'][provider]:
                raise ValueError('Provider cap exceeded; no transfer permitted')
            doc['calls'][call_id] = {'provider': provider, 'reserved_micro': maximum,
                                    'state': 'reserved', 'at': now(), 'fingerprint': fingerprint}

    def settle(self, call_id, actual):
        actual = micro(actual)
        with self.locked() as doc:
            row = doc['calls'][call_id]
            if row['state'] == 'settled':
                if row['actual_micro'] != actual:
                    raise ValueError('Conflicting settlement')
                return
            # Preserve recorded violations; do not roll the charge back out of the ledger.
            row.update(actual_micro=actual, state='settled', settled_at=now(),
                       over_reservation=actual > row['reserved_micro'])


def credential(provider):
    env = 'TREG_TOKEN' if provider == 'treg' else 'OPENROUTER_API_KEY'
    if os.environ.get(env):
        return os.environ[env].strip()
    file_env = 'TREG_KEY_FILE' if provider == 'treg' else 'OPENROUTER_KEY_FILE'
    configured = os.environ.get(file_env)
    config_setting = os.environ.get('OUTREACH_CONFIG')
    config_path = Path(config_setting) if config_setting else None
    if not configured and config_path and config_path.is_file():
        configured = read(config_path).get('treg_key_file' if provider == 'treg' else 'openrouter_key_file')
    if not configured:
        raise ValueError('Missing ' + env + ' or ' + file_env)
    path = Path(configured)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Credential file must be user-owned regular file with permissions 0600')
    key = path.read_text().strip()
    if not key:
        raise ValueError('Empty credential')
    return key


def http(url, headers=None, body=None):
    request = Request(url, json.dumps(body).encode() if body is not None else None,
                      headers or {}, method='POST' if body is not None else 'GET')
    with urlopen(request, timeout=60) as response:
        return json.load(response), dict(response.headers)


def preflight():
    key = credential('openrouter')
    models, _ = http('https://openrouter.ai/api/v1/models/' + MODEL + '/endpoints')
    data = models['data']
    endpoints = data.get('endpoints', [])
    model = endpoints[0] if data.get('id') == MODEL and len(endpoints) == 1 else None
    if not model or not model.get('pricing') or model.get('context_length') != 32000:
        raise ValueError('Expected single Jev endpoint/pricing/context unavailable')
    pricing = model['pricing']
    rates = [Decimal(pricing[field]) for field in ('prompt', 'completion')]
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise ValueError('Invalid model price')
    # Reject unexpected fixed/per-request fees rather than silently ignore them.
    if any(Decimal(str(value)) != 0 for field, value in pricing.items()
           if field not in ('prompt', 'completion') and value is not None):
        raise ValueError('Unexpected Jev pricing component')
    credits, _ = http('https://openrouter.ai/api/v1/credits', {'Authorization': 'Bearer ' + key})
    remaining = Decimal(str(credits['data']['total_credits'])) - Decimal(str(credits['data']['total_usage']))
    cap = sum(rates) * 32000
    if remaining < cap:
        raise ValueError('Insufficient OpenRouter remaining credit')
    return {'model': MODEL, 'pricing': pricing, 'context_length': 32000,
            'remaining_credit_usd': str(remaining), 'reserve_usd': str(cap), 'checked_at': now()}


def call(provider, request, output, ledger, call_id):
    output = Path(output)
    if output.exists():
        raise ValueError('Output exists; inspect saved response instead of repeating a call')
    key = credential(provider)
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if provider == 'openrouter':
        check = preflight()
        body = {'model': MODEL, 'state': request['state'], 'questions': request['questions']}
        if len(json.dumps(body).encode()) > 24000:
            raise ValueError('Jev request too large for conservative token envelope')
        maximum = check['reserve_usd']
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
        url = API
    else:
        maximum = request['max_usd']
        endpoint = request['endpoint_id']
        if not all(c.isalnum() or c in '.-_' for c in endpoint):
            raise ValueError('Unsafe endpoint ID')
        headers = {'X-Treg-Token': key, 'Content-Type': 'application/json',
                   'X-Treg-Route-Max-Cost': str(maximum), 'Idempotency-Key': call_id}
        url = 'https://treg.to/call/' + endpoint
        if request.get('method', 'POST') == 'GET':
            url += '?' + urlencode(request['params'], doseq=True)
            body = None
        elif request.get('method', 'POST') == 'POST':
            body = request['params']
        else:
            raise ValueError('Research adapters only support GET/POST')
    ledger.reserve(provider, call_id, maximum, fingerprint)
    # Any transport error leaves a hold. No automatic retry or assumed free failure.
    body, response_headers = http(url, headers, body)
    cost = body.get('usage', {}).get('cost') if provider == 'openrouter' else next(
        (Decimal(value) / 1000000 for name, value in response_headers.items()
         if name.lower() == 'x-treg-cost-micro'), None)
    recording = {'provider': provider, 'body': body, 'cost_usd': str(cost) if cost is not None else None,
                 'local_call_id': call_id, 'retrieved_at': now(), 'request_fingerprint': fingerprint,
                 'call_id': body.get('id') if provider == 'openrouter' else next(
                     (value for name, value in response_headers.items() if name.lower() == 'x-treg-call-id'), None)}
    if provider == 'openrouter':
        recording['preflight'] = check
    write(output, recording)
    if cost is None:
        raise ValueError('Missing actual cost: recording saved, reservation retained')
    ledger.settle(call_id, cost)
    if micro(cost) > micro(maximum):
        raise ValueError('Provider exceeded reservation; recorded charge, halt further dispatch')
    return recording


def profile(value):
    parsed = urlparse(value or '')
    parts = parsed.path.strip('/').split('/')
    if parsed.scheme == 'https' and parsed.hostname in ('linkedin.com', 'www.linkedin.com') and len(parts) == 2 and parts[0] == 'in':
        return 'https://www.linkedin.com/in/' + parts[1].lower()
    return None


def normalize(recording, domain):
    rows = recording.get('body', recording).get('output', {}).get('people', [])
    result = {}
    for row in rows:
        company = row.get('company') or {}
        company = company if isinstance(company, dict) else {}
        website = row.get('lastCompanyWebsite') or row.get('company_url') or company.get('website')
        host = urlparse(website if '://' in (website or '') else 'https://' + (website or '')).hostname
        url = profile(row.get('profileUrl') or row.get('linkedin_url') or row.get('employee_linkedin'))
        name = row.get('fullName') or row.get('full_name') or ' '.join(filter(None, [
            row.get('firstname') or row.get('first_name'), row.get('lastname') or row.get('last_name')]))
        title = row.get('lastJobTitle') or row.get('current_title') or row.get('title')
        if not url or not name or not title or (host or '').removeprefix('www.') != domain:
            continue
        result.setdefault(url, {'candidate_id': hashlib.sha256(url.encode()).hexdigest()[:12],
                                'name': name, 'role': title, 'profile_url': url,
                                'identity_status': 'lead_only', 'provider_row': row})
    return list(result.values())


def questions():
    return {'role_fit': {'type': 'score', 'instructions': 'Judge current professional relevance to the supplied purpose using evidence only.',
                         'criteria': ['No relevant current work', 'Adjacent work', 'Directly relevant current work']},
            'route': {'type': 'choice', 'instructions': 'Which CURRENT professional role is evidenced?',
                      'criteria': {'peer': 'Could offer relevant peer insight', 'hiring': 'Relevant recruiting or people leader', 'neither': 'Neither role is evidenced'}},
            'shared_context': {'type': 'score', 'instructions': 'Judge evidence-backed professional overlap with sender, never infer personal ties.',
                               'criteria': ['No specific overlap', 'Plausible overlap', 'Specific supported overlap']}}


def email_plan(spec, people):
    domain = spec['domain'].lower().removeprefix('www.')
    if not domain or '/' in domain or '@' in domain:
        raise ValueError('Expected company domain')
    requests = []
    for person in people:
        if person.get('held'):
            continue
        url = profile(person.get('profile_url') or person.get('channel_url'))
        name = person.get('name', '').split(',')[0].strip()
        if not url or not name:
            raise ValueError('Email lookup requires selected name and professional profile')
        requests.append({'candidate_id': person.get('candidate_id'), 'expected_name': name,
                         'endpoint_id': 'treg.people.email.find', 'method': 'POST',
                         'params': {'full_name': name, 'domain': domain, 'linkedin_url': url}, 'max_usd': '0.05'})
    if not 1 <= len(requests) <= 2:
        raise ValueError('Select one or two eligible professionals')
    return requests


def found_email(recording):
    return recording.get('body', recording).get('output', {}).get('email')


def verify_plan(recording):
    email = found_email(recording)
    if not isinstance(email, str) or email.count('@') != 1 or any(c.isspace() for c in email):
        raise ValueError('No provider-returned address to verify')
    return {'endpoint_id': 'treg.people.email.verify', 'method': 'POST',
            'params': {'email': email}, 'max_usd': '0.02'}


def email_assess(person, finding, verifications):
    email = found_email(finding)
    if not email:
        return {'email': None, 'mailbox_status': 'not_found', 'requires_review': True}
    output = finding['body']['output']
    finder_raw = finding['body'].get('raw', {})
    observed_name = ' '.join(filter(None, [output.get('first_name'), output.get('last_name')])) or finder_raw.get('fullName', '')
    expected = person['name'].split(',')[0].strip().casefold()
    identity_mismatch = bool(observed_name and observed_name.casefold() != expected)
    checks = []
    for recording in verifications:
        body = recording.get('body', recording)
        row = body.get('output', body.get('data', body))
        raw = body.get('raw', row)
        returned_email = row.get('email') or raw.get('email')
        if returned_email and returned_email.casefold() != email.casefold():
            raise ValueError('Verifier recording belongs to another mailbox')
        status = row.get('status', row.get('validity', row.get('result', 'unknown')))
        verification = row.get('verification') or raw.get('verification') or {}
        checks.append({'provider_call_id': recording.get('call_id'), 'status': status,
                       'smtp_checked': row.get('smtp_check', raw.get('validSMTP')),
                       'accept_all': row.get('accept_all'), 'verified_at': verification.get('date'),
                       'identity_valid': raw.get('validIdentity'),
                       'reason': raw.get('reason'), 'retrieved_at': recording.get('retrieved_at')})
    smtp_valid = any(c['status'] == 'valid' and c['smtp_checked'] is True and c['accept_all'] is False for c in checks)
    # Identity-only valid-risky/unknown and a later positive SMTP check are
    # complementary observations. A rejection remains a genuine conflict.
    rejection = any(c['status'] in ('invalid', 'undeliverable') or c['smtp_checked'] is False or c['identity_valid'] is False for c in checks)
    conflict = smtp_valid and rejection
    return {'email': email, 'finder_call_id': finding.get('call_id'), 'finder_verified': output.get('verified'),
            'finder_name': observed_name or None, 'expected_name': person['name'],
            'identity_mismatch': identity_mismatch, 'checks': checks,
            'mailbox_status': 'provider_reported_deliverable' if smtp_valid else 'unconfirmed_or_rejected',
            'mailbox_verified': smtp_valid, 'verification_conflict': conflict,
            'requires_review': identity_mismatch or conflict or not smtp_valid,
            'note': 'Mailbox deliverability is separate from ownership; preserve dated/cached checks and reconfirm before sending.'}


def x_handle(url):
    parsed = urlparse(url or '')
    parts = parsed.path.strip('/').split('/')
    if parsed.scheme != 'https' or parsed.hostname not in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com') or len(parts) != 1:
        raise ValueError('Expected exact public X profile URL')
    handle = parts[0]
    if not 1 <= len(handle) <= 15 or not all(c.isascii() and (c.isalnum() or c == '_') for c in handle) or handle.lower() in ('home', 'search', 'intent', 'i', 'explore'):
        raise ValueError('Invalid X handle')
    return handle.lower()


def x_plan(person):
    handle = x_handle(person.get('x_profile_url'))
    sources = person.get('x_identity_sources', [])
    if len({s.get('url') for s in sources}) < 2 or not all(s.get('supports_same_person') and s.get('url', '').startswith('https://') for s in sources):
        raise ValueError('X account needs two corroborating professional identity sources')
    return {'endpoint_id': 'treg.x.user.posts', 'method': 'POST', 'params': {'username': handle}, 'max_usd': '0.02'}


def x_normalize(person, recording, retrieved_at):
    handle = x_plan(person)['params']['username']
    retrieved = dt.datetime.fromisoformat(retrieved_at.replace('Z', '+00:00'))
    if retrieved.tzinfo is None:
        raise ValueError('Retrieval timestamp needs timezone')
    rows = recording.get('body', recording).get('output', {}).get('posts', [])
    posts = []; held = []
    for row in rows:
        if str(row.get('authorHandle', '')).lstrip('@').casefold() != handle:
            held.append({'post_id': row.get('id'), 'reason': 'author mismatch'}); continue
        published = None
        if row.get('createdUtc') is not None:
            published = dt.datetime.fromtimestamp(row['createdUtc'], dt.timezone.utc)
            if published > retrieved:
                held.append({'post_id': row.get('id'), 'reason': 'future publication timestamp'}); continue
        post_id = str(row.get('id', ''))
        if not post_id.isdigit():
            held.append({'post_id': post_id, 'reason': 'missing canonical post id'}); continue
        age = (retrieved - published).total_seconds() / 86400 if published else None
        posts.append({'url': 'https://x.com/' + handle + '/status/' + post_id,
                      'author_handle': handle, 'text': row.get('text', ''),
                      'published_at': published.isoformat() if published else None,
                      'retrieved_at': retrieved_at, 'within_30_days': age is not None and age <= 30,
                      'is_reply': row.get('isReply'), 'source_call_id': recording.get('call_id'),
                      'professional_relevance': 'host_review_required'})
    return {'profile_url': 'https://x.com/' + handle, 'posts': posts, 'held': held,
            'recent_posts_in_returned_page': sum(p['within_30_days'] for p in posts),
            'dm_availability': 'unknown', 'more_active_than_linkedin': 'not_established'}


def rank(candidates, responses):
    output = []
    for person in candidates:
        response = responses[person['candidate_id']]
        a = response.get('body', response)['answers']
        route = a['route']
        if route.get('type') != 'choice' or route.get('choice') not in ('peer', 'hiring', 'neither'):
            raise ValueError('Invalid Jev route')
        probabilities = route.get('probabilities')
        if not probabilities or set(probabilities) != {'peer', 'hiring', 'neither'} or any(
                not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in probabilities.values()):
            raise ValueError('Invalid Jev probabilities')
        if abs(sum(probabilities.values()) - 1) > .02:
            raise ValueError('Jev probabilities do not sum to one')
        for field in ('role_fit', 'shared_context'):
            if a[field].get('type') != 'score' or not isinstance(a[field].get('score'), (int, float)) or not math.isfinite(a[field]['score']) or not 0 <= a[field]['score'] <= 2:
                raise ValueError('Invalid Jev score')
        output.append({**person, 'lane': route['choice'], 'score': .7 * a['role_fit']['score'] + .3 * a['shared_context']['score'],
                       'held': route['choice'] == 'neither' or probabilities['neither'] >= .2,
                       'jev_response': response})
    return sorted(output, key=lambda row: (row['held'], -row['score']))


def compile_result(spec, evidence):
    contacts = evidence.get('contacts', [])
    if len(contacts) > 2:
        raise ValueError('Select at most two contacts')
    for contact in contacts:
        for key in ('name', 'role', 'relevance', 'channel', 'channel_reason', 'draft'):
            if not contact.get(key):
                raise ValueError('Missing contact ' + key)
        sources = contact.get('identity_sources', [])
        if len({row.get('url') for row in sources}) < 2 or any(
                not row.get('supports_current_role') for row in sources):
            raise ValueError('Current identity requires two corroborating sources')
        if contact['channel'] == 'email' and contact.get('mailbox_verified') is not True:
            raise ValueError('Email channel requires mailbox verification')
        if contact['channel'] == 'email' and contact.get('email_assessment', {}).get('requires_review'):
            raise ValueError('Email identity or verification conflict requires review')
        for source in contact.get('evidence', []) + sources:
            if not source.get('url', '').startswith('https://') or not source.get('retrieved_at') or 'published_at' not in source:
                raise ValueError('Evidence needs HTTPS URL and date fields')
            retrieved = dt.datetime.fromisoformat(source['retrieved_at'].replace('Z', '+00:00'))
            if retrieved.tzinfo is None:
                raise ValueError('Retrieval timestamp needs a timezone')
            if source['published_at'] is not None:
                published = dt.datetime.fromisoformat(source['published_at'].replace('Z', '+00:00'))
                age = (retrieved.date() - published.date()).days
                if age < 0:
                    raise ValueError('Publication date is after retrieval')
                source['recency_status'] = 'within_90_days' if age <= 90 else 'historical'
            else:
                source['recency_status'] = 'unknown'
        if not contact.get('evidence'):
            raise ValueError('No personalization evidence')
    if not evidence.get('review'):
        raise ValueError('Host source/voice review required')
    return {**evidence, 'schema': 'standalone-outreach.v1', 'source_snapshot_sha256': spec.get('source_snapshot_sha256'),
            'sending_enabled': False, 'contacts': contacts, 'compiled_at': now()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, default=Path('.outreach/budget.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('preflight')
    p = sub.add_parser('init'); p.add_argument('--treg-usd', required=True); p.add_argument('--openrouter-usd', required=True); p.add_argument('--authorization', required=True)
    sub.add_parser('budget-status')
    p = sub.add_parser('reserve'); p.add_argument('provider'); p.add_argument('call_id'); p.add_argument('maximum')
    p = sub.add_parser('settle'); p.add_argument('call_id'); p.add_argument('actual')
    p = sub.add_parser('call'); p.add_argument('provider', choices=['treg', 'openrouter']); p.add_argument('request'); p.add_argument('--out', required=True); p.add_argument('--call-id', default=None)
    p = sub.add_parser('plan'); p.add_argument('spec'); p.add_argument('--out-dir', required=True)
    p = sub.add_parser('compile'); p.add_argument('spec'); p.add_argument('evidence'); p.add_argument('--out-dir', required=True)
    p = sub.add_parser('normalize'); p.add_argument('recording'); p.add_argument('domain'); p.add_argument('--out', required=True)
    p = sub.add_parser('rank'); p.add_argument('candidates'); p.add_argument('responses'); p.add_argument('--out', required=True)
    p = sub.add_parser('email-plan'); p.add_argument('spec'); p.add_argument('selected'); p.add_argument('--out', required=True)
    p = sub.add_parser('verify-plan'); p.add_argument('finding'); p.add_argument('--out', required=True)
    p = sub.add_parser('email-assess'); p.add_argument('person'); p.add_argument('finding'); p.add_argument('verifications', nargs='+'); p.add_argument('--out', required=True)
    p = sub.add_parser('x-plan'); p.add_argument('person'); p.add_argument('--out', required=True)
    p = sub.add_parser('x-normalize'); p.add_argument('person'); p.add_argument('recording'); p.add_argument('--retrieved-at', required=True); p.add_argument('--out', required=True)
    args = parser.parse_args(); ledger = Budget(args.ledger)
    if args.command == 'preflight': print(json.dumps(preflight(), indent=2))
    elif args.command == 'init':
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        caps = {'treg': micro(args.treg_usd), 'openrouter': micro(args.openrouter_usd)}
        with args.ledger.open('x', encoding='utf-8') as handle:
            os.chmod(args.ledger, 0o600)
            json.dump({'version': 1, 'authorization': args.authorization, 'caps_micro': caps, 'calls': {}}, handle, indent=2)
    elif args.command == 'budget-status':
        doc = read(args.ledger)
        print(json.dumps({'caps_micro': doc['caps_micro'], 'used_or_held_micro': {
            provider: sum(row.get('actual_micro', row['reserved_micro']) for row in doc['calls'].values() if row['provider'] == provider)
            for provider in doc['caps_micro']}, 'unsettled_calls': [key for key, row in doc['calls'].items() if row['state'] != 'settled']}, indent=2))
    elif args.command == 'reserve': ledger.reserve(args.provider, args.call_id, args.maximum)
    elif args.command == 'settle': ledger.settle(args.call_id, args.actual)
    elif args.command == 'call':
        result = call(args.provider, read(args.request), args.out, ledger, args.call_id or str(uuid.uuid4()))
        print(json.dumps({key: result[key] for key in ('call_id', 'cost_usd', 'provider')}))
    elif args.command == 'normalize': write(args.out, normalize(read(args.recording), args.domain))
    elif args.command == 'rank': write(args.out, rank(read(args.candidates), read(args.responses)))
    elif args.command == 'email-plan': write(args.out, email_plan(read(args.spec), read(args.selected)))
    elif args.command == 'verify-plan': write(args.out, verify_plan(read(args.finding)))
    elif args.command == 'email-assess': write(args.out, email_assess(read(args.person), read(args.finding), [read(p) for p in args.verifications]))
    elif args.command == 'x-plan': write(args.out, x_plan(read(args.person)))
    elif args.command == 'x-normalize': write(args.out, x_normalize(read(args.person), read(args.recording), args.retrieved_at))
    elif args.command == 'plan':
        spec = read(args.spec)
        for key in ('company', 'domain', 'purpose', 'sender_summary', 'search_titles'):
            if not spec.get(key): raise ValueError('Missing spec ' + key)
        if not 1 <= len(spec['search_titles']) <= 5: raise ValueError('Use 1-5 bounded searches')
        write(Path(args.out_dir) / 'plan.json', {'requests': [
            {'endpoint_id': 'treg.people.search', 'params': {'company_domain': spec['domain'], 'title': title, 'limit': 4}, 'max_usd': '0.03'}
            for title in spec['search_titles']], 'jev_model': MODEL, 'sending_enabled': False})
    elif args.command == 'compile':
        result = compile_result(read(args.spec), read(args.evidence)); out = Path(args.out_dir)
        write(out / 'result.json', result)
        text = '# Outreach brief — UNSENT\n\n'
        text += 'Status: ' + result.get('status', 'draft') + '. Sending disabled.\n\n'
        if result.get('source_snapshot_sha256'):
            text += 'Frozen input snapshot: `' + result['source_snapshot_sha256'] + '`.\n\n'
        if result.get('cost'):
            text += 'Exact external provider cost: $' + result['cost']['total_exact_usd'] + '. Native runtime costs are separate.\n\n'
        for c in result['contacts']:
            text += f"## {c['name']} — {c['role']}\n\n{c['relevance']}\n\nChannel: {c['channel']}. {c['channel_reason']}\n\n{c['draft']}\n\n"
            if c.get('follow_up_draft'):
                text += 'Follow-up draft, only after connection acceptance:\n\n' + c['follow_up_draft'] + '\n\n'
            if c.get('email_assessment'):
                text += 'Work email assessment:\n\n' + json.dumps(c['email_assessment'], ensure_ascii=False) + '\n\n'
            if c.get('x_profile_url'):
                text += 'X: [' + c['x_profile_url'] + '](' + c['x_profile_url'] + '). DM availability unverified.\n\n'
            if c.get('x_draft'):
                text += 'X DM draft (UNSENT; use only if private DMs are available):\n\n' + c['x_draft'] + '\n\n'
            if c.get('email_draft'):
                text += 'Email draft (UNSENT; resolve verification/identity holds before sending):\n\n' + c['email_draft'] + '\n\n'
            for source in c['evidence'] + c['identity_sources']:
                text += f"- [{source.get('claim', 'Evidence')}]({source['url']}); published {source['published_at'] or 'unknown'}, retrieved {source['retrieved_at']}\n"
            text += '\n'
        if result.get('limitations'):
            text += '## Limits and uncertainties\n\n' + ''.join('- ' + value + '\n' for value in result['limitations'])
        text += '\nReview: ' + json.dumps(result.get('review', {}), ensure_ascii=False) + '\n'
        (out / 'report.md').write_text(text, encoding='utf-8')


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Provider bodies/headers can contain secrets or private data. Print type only.
        detail = ': ' + str(exc) if isinstance(exc, (ValueError, KeyError)) else ''
        raise SystemExit('Stopped: ' + type(exc).__name__ + detail + '; no automatic retry')
