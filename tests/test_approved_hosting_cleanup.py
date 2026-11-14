"""The one-time cleanup cannot expand beyond the user's approved release list."""

import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from scripts import prune_approved_hosted_releases as cleanup


class ApprovedCleanupTests(unittest.TestCase):
    def plan(self):
        return dict(root=cleanup.deployment.ROOT.as_posix()+'/.releases',preserve=['a'*40,'b'*40,'c'*40],
                    active_release='a'*40,pending_release='b'*40,remove_candidates=['d'*40])

    def response(self,status=200,payload=None):
        response=MagicMock()
        response.__enter__.return_value=response
        response.status_code=status
        response.iter_content.return_value=[json.dumps(payload).encode()]
        return response

    def test_manifest_must_match_explicit_hash_and_stay_inside_release_root(self):
        plan=self.plan()
        content=json.dumps(plan).encode()
        self.assertEqual(cleanup.validate_plan(content,hashlib.sha256(content).hexdigest()),plan)
        with self.assertRaises(ValueError):
            cleanup.validate_plan(content,'0'*64)
        for field,value in [('root','/home/GauravChhajer/StudentConnect/data'),('remove_candidates',['../data']),('remove_candidates',['a'*40])]:
            invalid=dict(plan,**{field:value})
            content=json.dumps(invalid).encode()
            with self.assertRaises(ValueError):
                cleanup.validate_plan(content,hashlib.sha256(content).hexdigest())

    def test_deletes_only_exact_approved_path_after_checking_active_and_pending(self):
        session=MagicMock()
        session.request.side_effect=[self.response(payload={'sha':'a'*40}),self.response(payload={'sha':'b'*40}),self.response(204)]
        with patch.object(cleanup.time,'sleep'):
            self.assertEqual(cleanup.prune(session,self.plan()),dict(deleted=1,already_absent=0))
        calls=session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls],['GET','GET','DELETE'])
        self.assertTrue(calls[2].args[1].endswith('/.releases/'+'d'*40))
        self.assertFalse(calls[2].kwargs['allow_redirects'])

    def test_changed_active_or_pending_release_stops_before_deletion(self):
        for active,pending in [('e'*40,'b'*40),('a'*40,'e'*40)]:
            session=MagicMock()
            session.request.side_effect=[self.response(payload={'sha':active}),self.response(payload={'sha':pending})]
            with patch.object(cleanup.time,'sleep'),self.assertRaises(RuntimeError):
                cleanup.prune(session,self.plan())
            self.assertEqual([call.args[0] for call in session.request.call_args_list],['GET','GET'])

    def test_uncertain_delete_is_never_retried(self):
        session=MagicMock()
        session.request.side_effect=[self.response(payload={'sha':'a'*40}),self.response(payload={'sha':'b'*40}),cleanup.requests.ReadTimeout('private response')]
        with patch.object(cleanup.time,'sleep'),self.assertRaises(cleanup.requests.ReadTimeout):
            cleanup.prune(session,self.plan())
        self.assertEqual(sum(call.args[0]=='DELETE' for call in session.request.call_args_list),1)

    def test_protected_targets_are_blocked_even_if_caller_skips_manifest_validation(self):
        session=MagicMock()
        session.request.side_effect=[self.response(payload={'sha':'a'*40}),self.response(payload={'sha':'b'*40})]
        with patch.object(cleanup.time,'sleep'),self.assertRaises(ValueError):
            cleanup.prune(session,dict(self.plan(),remove_candidates=['a'*40]))
        self.assertFalse(any(call.args[0]=='DELETE' for call in session.request.call_args_list))


if __name__=='__main__':
    unittest.main()
