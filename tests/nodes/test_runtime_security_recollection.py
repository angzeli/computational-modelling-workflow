"""Native ACL/flag boundaries use the real collector, without a package restart."""
import os
import sys
import unittest
from unittest.mock import patch
from cmw.nodes import identity,native_security,runtime_observation as obs
from tests.nodes import test_runtime_observation as fixtures

@unittest.skipUnless(sys.platform=='darwin','Darwin native ACL/UF_TRACKED')
class RuntimeSecurityRecollectionTests(unittest.TestCase):
    setUp=fixtures.RuntimeObservationTests.setUp

    def test_transition_during_initial_acl_collection_is_verified(self):
        real=native_security.acl_evidence;events=[];member_id=self.member.stat().st_ino
        def acl(fd,**kwargs):
            result=real(fd,**kwargs)
            if os.fstat(fd).st_ino==member_id and not events:
                before=self.member.stat();os.chflags(self.member,before.st_flags^0x40)
                events.append((before.st_ctime_ns,self.member.stat().st_ctime_ns,result['state']))
            return result
        evidence=[]
        with patch.object(native_security,'acl_evidence',side_effect=acl):
            record,current=identity.runtime_observed(diagnostics=evidence)
        self.assertEqual(len(events),1)
        self.assertIn(events[0][2],{'absent','present'})
        self.assertEqual(record,self.original)
        self.assertEqual(current['member_sha256'],self.epoch['member_sha256'])
        self.assertEqual(evidence[0]['content_reads'],{'__init__.py':1})
        self.assertEqual(evidence[0]['transitions'][0]['phase'],'security-descriptor-after')

    def test_repeated_same_member_incoherence_exhausts_member_not_package_budget(self):
        real=native_security.acl_evidence;count=0;member_id=self.member.stat().st_ino
        def acl(fd,**kwargs):
            nonlocal count
            result=real(fd,**kwargs)
            if os.fstat(fd).st_ino==member_id:
                count+=1;os.chflags(self.member,self.member.stat().st_flags^0x40)
            return result
        with patch.object(native_security,'acl_evidence',side_effect=acl):
            with self.assertRaisesRegex(obs.RuntimeRefusal,'member_observation_budget'):identity.runtime_observed()
        self.assertEqual(count,2)

    def test_partial_security_evidence_and_unknown_flags_cannot_be_reclassified(self):
        with patch.object(obs,'_extended_security',return_value={'__cmw_acl__':'UNAVAILABLE:ACL'}):
            with self.assertRaisesRegex(obs.RuntimeRefusal,'security_metadata_incomplete'):identity.runtime_observed()
        real=native_security.acl_evidence;member_id=self.member.stat().st_ino
        def acl(fd,**kwargs):
            result=real(fd,**kwargs)
            if os.fstat(fd).st_ino==member_id:
                # Synthetic stat boundary avoids setting an immutable OS flag.
                value=list(native_security.object_epoch(os.fstat(fd)));value[9]^=0x42
                kwargs['observe'](tuple(value),'acl-synthetic-mixed')
            return result
        with patch.object(native_security,'acl_evidence',side_effect=acl):
            with self.assertRaisesRegex(obs.RuntimeRefusal,'in_flight_change'):identity.runtime_observed()

    def test_flag_transition_inside_native_acl_property_query_is_recorded(self):
        real=native_security._api();member_id=self.member.stat().st_ino;trigger=[]
        class DuringQuery:
            fd=None
            def __getattr__(self,name):return getattr(real,name)
            def fstatx_np(self,fd,*args):
                self.fd=fd
                return real.fstatx_np(fd,*args)
            def filesec_query_property(self,*args):
                result=real.filesec_query_property(*args)
                if self.fd is not None and os.fstat(self.fd).st_ino==member_id and not trigger:
                    os.chflags(self_file,self_file.stat().st_flags^0x40);trigger.append(True)
                return result
        self_file=self.member;evidence=[]
        with patch.object(native_security,'_api',return_value=DuringQuery()):
            record,_=identity.runtime_observed(diagnostics=evidence)
        self.assertEqual(record,self.original)
        self.assertEqual(evidence[0]['transitions'][0]['phase'],'acl-after')
        self.assertEqual(len(trigger),1)
