"""Real collector, fixed scheduling controls and handwritten safety expectations."""
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from cmw.nodes import identity,runtime_observation as obs
from tests.nodes import test_runtime_observation as fixtures

@unittest.skipUnless(sys.platform=='darwin','Darwin metadata policy')
class CollectionEvidenceTests(unittest.TestCase):
    setUp=fixtures.RuntimeObservationTests.setUp

    def test_staggered_members_before_open_and_final_inventory_are_verified_without_restart(self):
        a=self.member;b=self.package/'second.py';b.write_text('second=2\n')
        original,baseline=identity.runtime_observed();events=[];real_read=obs.RuntimeCollection.read
        real_inventory=identity.inventory
        def read(collection,path):
            if path==a and collection.generation=='content' and not events:
                os.chflags(a,a.stat().st_flags^0x40);events.append('A-before-open')
            return real_read(collection,path)
        def inventory(root,**kwargs):
            if kwargs.get('final'):
                os.chflags(b,b.stat().st_flags^0x40);events.append('B-before-final')
            return real_inventory(root,**kwargs)
        evidence=[]
        with patch.object(obs.RuntimeCollection,'read',read),patch.object(identity,'inventory',side_effect=inventory) as scans:
            record,current=identity.runtime_observed(diagnostics=evidence)
        self.assertEqual(scans.call_count,2)
        self.assertEqual(events,['A-before-open','B-before-final'])
        self.assertEqual(evidence[0]['content_reads'],{'__init__.py':1,'second.py':2})
        self.assertEqual(len(evidence[0]['transitions']),2)
        self.assertEqual(evidence[0]['status'],'COMPLETE')
        self.assertEqual(record,original)
        self.assertEqual(current['member_sha256'],{'__init__.py':hashlib.sha256(b'value=1\n').hexdigest(),
                                                   'second.py':hashlib.sha256(b'second=2\n').hexdigest()})
        self.assertEqual(evidence[0]['bytes_read'],len(b'value=1\n')+2*len(b'second=2\n'))

    def test_late_hidden_content_change_with_flag_cannot_use_old_hash(self):
        real=identity.inventory
        def inventory(root,**kwargs):
            if kwargs.get('final'):
                before=self.member.stat();self.member.write_text('value=2\n')
                os.utime(self.member,ns=(before.st_atime_ns,before.st_mtime_ns))
                os.chflags(self.member,self.member.stat().st_flags^0x40)
            return real(root,**kwargs)
        with patch.object(identity,'inventory',side_effect=inventory):
            with self.assertRaisesRegex(obs.RuntimeRefusal,'content_changed'):identity.runtime_observed()

    def test_material_change_then_restore_is_terminal_at_observed_boundary(self):
        real=obs.RuntimeCollection.object;seen=[]
        def observe(collection,path,value,phase):
            if path==self.member and phase=='content-open' and not seen:
                seen.append(True);bad=list(value);bad[2]^=0o020
                try:real(collection,path,tuple(bad),phase)
                finally:os.chmod(path,path.stat().st_mode)  # restoration cannot erase recorded refusal
            return real(collection,path,value,phase)
        evidence=[]
        with patch.object(obs.RuntimeCollection,'object',observe):
            with self.assertRaises(obs.RuntimeRefusal) as caught:identity.runtime_observed(diagnostics=evidence)
        self.assertEqual(caught.exception.evidence['phase'],'content-open')
        self.assertEqual(caught.exception.evidence['changed_indices'],[2])
        self.assertEqual(evidence[0]['status'],'REFUSED')

    def test_deadline_and_total_byte_budget_refuse_before_publication(self):
        for field,value,category in [('MAX_OBSERVATION_SECONDS',-1,'observation_deadline'),
                                     ('MAX_READ_BYTES',0,'member_size_bound')]:
            with patch.object(obs,field,value),self.assertRaisesRegex(obs.RuntimeRefusal,category):
                identity.runtime_observed()

    def test_membership_and_unqualified_root_directory_interpreter_changes_refuse(self):
        real=identity.inventory
        for kind in ('member','root','directory','interpreter'):
            def inventory(root,**kwargs):
                result=real(root,**kwargs)
                if kwargs.get('final'):
                    if kind=='member':return result[0],(),result[2]
                    if kind=='root':
                        changed=list(result[0]);changed[5]^=0x40;return tuple(changed),result[1],result[2]
                    if kind=='directory':return result[0],result[1],{'new':result[0]}
                return result
            native=identity.file_epoch;calls=0
            def executable(path,**kwargs):
                nonlocal calls
                value=native(path,**kwargs);calls+=1
                if kind=='interpreter' and calls>1:
                    value=list(value);value[4]+=1;return tuple(value)
                return value
            with self.subTest(kind=kind),patch.object(identity,'inventory',side_effect=inventory),patch.object(identity,'file_epoch',side_effect=executable):
                with self.assertRaises(obs.RuntimeRefusal):identity.runtime_observed()

    def test_incomplete_content_read_cannot_publish_partial_hashes(self):
        real=obs.os.fdopen
        class Short:
            def __init__(self,handle):self.handle=handle
            def __enter__(self):return self
            def __exit__(self,*args):self.handle.close()
            def fileno(self):return self.handle.fileno()
            def read(self,bound):return self.handle.read(bound)[:-1]
        evidence=[]
        with patch.object(obs.os,'fdopen',side_effect=lambda *a,**k:Short(real(*a,**k))):
            with self.assertRaisesRegex(obs.RuntimeRefusal,'incomplete_content_read'):
                identity.runtime_observed(diagnostics=evidence)
        self.assertEqual(evidence[0]['status'],'REFUSED')

    def test_content_enclosing_transition_forces_one_member_reread(self):
        real=obs.RuntimeCollection.object;events=[]
        def observe(collection,path,value,phase):
            if path==self.member and phase=='content-after' and not events:
                os.chflags(path,path.stat().st_flags^0x40);events.append(True)
                value=__import__('cmw.nodes.native_security',fromlist=['object_epoch']).object_epoch(path.stat())
            return real(collection,path,value,phase)
        evidence=[]
        with patch.object(obs.RuntimeCollection,'object',observe):
            record,_=identity.runtime_observed(diagnostics=evidence)
        self.assertEqual(record,self.original)
        self.assertEqual(evidence[0]['content_reads'],{'__init__.py':2})

    def test_observed_owner_and_xattr_changes_cannot_hide_behind_tracked(self):
        real=obs.RuntimeCollection.object
        for field,delta in [(3,1),(2,0o020),(9,0x42)]:
            def observe(collection,path,value,phase):
                if path==self.member and phase=='content-open':
                    bad=list(value);bad[field]^=delta;bad[9]^=0x40
                    return real(collection,path,tuple(bad),phase)
                return real(collection,path,value,phase)
            with self.subTest(field=field),patch.object(obs.RuntimeCollection,'object',observe):
                with self.assertRaises(obs.RuntimeRefusal):identity.runtime_observed()
        real_security=obs._extended_security;reads=0
        def security(path,**kwargs):
            nonlocal reads
            value=real_security(path,**kwargs)
            if path==self.member:
                reads+=1
                if reads==2:return {**value,'security.material.fixture':'changed'}
            return value
        with patch.object(obs,'_extended_security',side_effect=security):
            with self.assertRaisesRegex(obs.RuntimeRefusal,'security_metadata_changed'):identity.runtime_observed()

    def test_actual_added_removed_members_during_final_scan_refuse(self):
        real=identity.inventory
        for kind in ('added','removed'):
            def inventory(root,**kwargs):
                if kwargs.get('final'):
                    if kind=='added':(self.package/'added.py').write_text('new=1\n')
                    else:self.member.unlink()
                return real(root,**kwargs)
            with self.subTest(kind=kind),patch.object(identity,'inventory',side_effect=inventory):
                with self.assertRaises(obs.RuntimeRefusal):identity.runtime_observed()
            (self.package/'added.py').unlink(missing_ok=True)

    def test_actual_acl_change_with_tracked_refuses_original_authority(self):
        import subprocess
        subprocess.run(['/bin/chmod','+a','everyone allow read',str(self.member)],check=True,capture_output=True)
        os.chflags(self.member,self.member.stat().st_flags^0x40)
        verifier=obs.RuntimeVerifier(self.original,self.epoch,isolated=True)
        with self.assertRaisesRegex(obs.RuntimeRefusal,'security_metadata_changed'):
            verifier.verify(identity.runtime_observed)
