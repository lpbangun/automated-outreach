import concurrent.futures
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import outreach as o


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'budget.json'
        o.write(self.ledger, {'caps_micro': {'treg': 250000, 'openrouter': 250000}, 'calls': {}})

    def test_concurrent_reservations_cannot_overspend(self):
        def reserve(index):
            try:
                o.Budget(self.ledger).reserve('treg', str(index), '.10')
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(8))), 2)

    def test_unknown_dispatch_holds_funds_and_blocks_duplicate(self):
        b = o.Budget(self.ledger)
        b.reserve('treg', 'one', '.25')
        with self.assertRaises(ValueError): b.reserve('treg', 'one', '.25')
        with self.assertRaises(ValueError): b.reserve('treg', 'two', '.01')
        b.reserve('openrouter', 'separate', '.25')
        b.settle('one', '.01')
        b.reserve('treg', 'two', '.24')

    def test_micro_costs_round_up_and_reject_invalid(self):
        self.assertEqual(o.micro('0.00000001'), 1)
        for value in ('NaN', 'Infinity', '-.01'):
            with self.assertRaises(ValueError): o.micro(value)

    def test_overcharge_is_retained_and_freezes_further_dispatch(self):
        b = o.Budget(self.ledger); b.reserve('treg', 'one', '.01'); b.settle('one', '.02')
        self.assertEqual(o.read(self.ledger)['calls']['one']['actual_micro'], 20000)
        with self.assertRaises(ValueError): b.reserve('openrouter', 'two', '.01')

    def test_missing_cost_retains_reservation_and_saves_response(self):
        with patch.object(o, 'credential', return_value='not-a-real-key'), patch.object(o, 'http', return_value=({'output': {}}, {})):
            with self.assertRaises(ValueError):
                o.call('treg', {'endpoint_id': 'treg.people.search', 'params': {}, 'max_usd': '.03'}, self.root / 'response.json', o.Budget(self.ledger), 'missing-cost')
        self.assertTrue((self.root / 'response.json').exists())
        self.assertEqual(o.read(self.ledger)['calls']['missing-cost']['state'], 'reserved')
        self.assertNotIn('not-a-real-key', (self.root / 'response.json').read_text())

    def test_same_name_different_profiles_not_merged_wrong_domain_held(self):
        rows = [{'fullName': 'Sam Example', 'lastJobTitle': 'People Lead', 'lastCompanyWebsite': 'example.com', 'profileUrl': 'https://www.linkedin.com/in/sam-one'},
                {'fullName': 'Sam Example', 'lastJobTitle': 'People Lead', 'lastCompanyWebsite': 'example.com', 'profileUrl': 'https://www.linkedin.com/in/sam-two'},
                {'fullName': 'Sam Example', 'lastJobTitle': 'People Lead', 'lastCompanyWebsite': 'other.example', 'profileUrl': 'https://www.linkedin.com/in/sam-three'}]
        result = o.normalize({'output': {'people': rows}}, 'example.com')
        self.assertEqual(len(result), 2)
        self.assertTrue(all(p['identity_status'] == 'lead_only' for p in result))

    def evidence(self):
        source = {'url': 'https://example.com/team', 'retrieved_at': '2026-10-02T00:00:00Z', 'published_at': None, 'supports_current_role': True}
        return {'review': {'source_truth': 'checked by host'}, 'contacts': [{
            'name': 'Sam Example', 'role': 'People Lead', 'relevance': 'Relevant hiring function',
            'channel': 'linkedin', 'channel_reason': 'Public professional profile',
            'draft': 'Hi Sam, could I ask about onboarding priorities?',
            'identity_sources': [source, {**source, 'url': 'https://example.org/sam'}], 'evidence': [source]}]}

    def test_compile_enforces_identity_dates_and_email_gate(self):
        e = self.evidence()
        result = o.compile_result({'source_snapshot_sha256': 'synthetic'}, e)
        self.assertFalse(result['sending_enabled'])
        self.assertIsNone(result['contacts'][0]['evidence'][0]['published_at'])
        for mutation in ('identity', 'email', 'date', 'review'):
            e = self.evidence()
            if mutation == 'identity': e['contacts'][0]['identity_sources'].pop()
            elif mutation == 'email': e['contacts'][0]['channel'] = 'email'
            elif mutation == 'date': e['contacts'][0]['evidence'][0]['retrieved_at'] = 'yesterday'
            else: del e['review']
            with self.assertRaises(ValueError): o.compile_result({}, e)

    def test_jev_neither_probability_holds_contact(self):
        a = {'role_fit': {'type': 'score', 'score': 2}, 'shared_context': {'type': 'score', 'score': 1},
             'route': {'type': 'choice', 'choice': 'hiring', 'probabilities': {'peer': .1, 'hiring': .6, 'neither': .3}}}
        r = o.rank([{'candidate_id': 'synthetic'}], {'synthetic': {'answers': a}})
        self.assertTrue(r[0]['held'])
        a['role_fit']['score'] = float('nan')
        with self.assertRaises(ValueError): o.rank([{'candidate_id': 'synthetic'}], {'synthetic': {'answers': a}})

    def test_undated_is_not_recent_and_future_publication_rejected(self):
        result = o.compile_result({}, self.evidence())
        self.assertEqual(result['contacts'][0]['evidence'][0]['recency_status'], 'unknown')
        e = self.evidence(); e['contacts'][0]['evidence'][0]['published_at'] = '2027-01-01'
        with self.assertRaises(ValueError): o.compile_result({}, e)

    def test_credential_file_permissions_and_symlink(self):
        key = self.root / 'key'; key.write_text('synthetic-key'); key.chmod(0o600)
        with patch.dict(os.environ, {'TREG_KEY_FILE': str(key)}, clear=True):
            self.assertEqual(o.credential('treg'), 'synthetic-key')
            key.chmod(0o644)
            with self.assertRaises(ValueError): o.credential('treg')
            key.chmod(0o600)
            link = self.root / 'link'; link.symlink_to(key)
            os.environ['TREG_KEY_FILE'] = str(link)
            with self.assertRaises(ValueError): o.credential('treg')

    def test_email_plan_selected_people_and_returned_address_only(self):
        people = [{'candidate_id': 's', 'name': 'Sam Example, MBA', 'profile_url': 'https://www.linkedin.com/in/sam-example'},
                  {'name': 'Held Person', 'held': True}]
        plan = o.email_plan({'domain': 'example.com'}, people)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]['params']['full_name'], 'Sam Example')
        with self.assertRaises(ValueError): o.verify_plan({'body': {'output': {}}})
        self.assertEqual(o.verify_plan({'body': {'output': {'email': 'sam@example.com'}}})['params']['email'], 'sam@example.com')

    def test_email_valid_mailbox_does_not_resolve_identity_or_conflict(self):
        person = {'name': 'Sam Example'}
        finding = {'call_id': 'f', 'body': {'output': {'email': 'sam@example.com', 'first_name': 'Sam', 'last_name': 'Mba', 'verified': True}}}
        smtp = {'call_id': 'v', 'body': {'data': {'email': 'sam@example.com', 'status': 'valid', 'smtp_check': True, 'accept_all': False, 'verification': {'date': '2026-09-03'}}}}
        identity = {'call_id': 'i', 'body': {'output': {'status': 'invalid'}, 'raw': {'email': 'sam@example.com', 'validSMTP': None}}}
        assessed = o.email_assess(person, finding, [identity, smtp])
        self.assertTrue(assessed['mailbox_verified'])
        self.assertTrue(assessed['identity_mismatch'])
        self.assertTrue(assessed['verification_conflict'])
        self.assertTrue(assessed['requires_review'])
        self.assertEqual(assessed['checks'][1]['verified_at'], '2026-09-03')
        smtp['body']['data']['email'] = 'other@example.com'
        with self.assertRaises(ValueError): o.email_assess(person, finding, [smtp])

    def x_person(self):
        return {'name': 'Sam Example', 'x_profile_url': 'https://x.com/sam_example', 'x_identity_sources': [
            {'url': 'https://example.com/sam', 'supports_same_person': True},
            {'url': 'https://example.org/interview', 'supports_same_person': True}]}

    def test_native_identity_check_and_smtp_confirmation_are_complementary(self):
        finding = {'body': {'output': {'email': 'sam@example.com', 'verified': False}, 'raw': {'fullName': 'Sam Example'}}}
        identity = {'body': {'email': 'sam@example.com', 'validity': 'valid-risky', 'validIdentity': True, 'validSMTP': None}}
        smtp = {'body': {'data': {'email': 'sam@example.com', 'status': 'valid', 'smtp_check': True, 'accept_all': False}}}
        assessed = o.email_assess({'name': 'Sam Example'}, finding, [identity, smtp])
        self.assertFalse(assessed['identity_mismatch'])
        self.assertFalse(assessed['verification_conflict'])
        self.assertFalse(assessed['requires_review'])
        self.assertEqual(assessed['checks'][0]['status'], 'valid-risky')
        self.assertTrue(assessed['checks'][0]['identity_valid'])

    def test_x_identity_author_dates_and_dm_availability(self):
        person = self.x_person()
        recording = {'call_id': 'x', 'body': {'output': {'posts': [
            {'id': '123', 'authorHandle': 'sam_example', 'createdUtc': 1790899200, 'text': 'Team workflow update'},
            {'id': '124', 'authorHandle': 'different_sam', 'createdUtc': 1790899200, 'text': 'Wrong identity'},
            {'id': '125', 'authorHandle': 'sam_example', 'createdUtc': 4070908800, 'text': 'Future post'}]}}}
        result = o.x_normalize(person, recording, '2026-10-02T23:00:00Z')
        self.assertEqual(len(result['posts']), 1)
        self.assertEqual(len(result['held']), 2)
        self.assertEqual(result['recent_posts_in_returned_page'], 1)
        self.assertEqual(result['dm_availability'], 'unknown')
        person['x_identity_sources'].pop()
        with self.assertRaises(ValueError): o.x_plan(person)
        with self.assertRaises(ValueError): o.x_handle('https://x.com/search')


if __name__ == '__main__': unittest.main()
