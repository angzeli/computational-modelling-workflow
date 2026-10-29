"""Native decoder ordering: receipt alone is not completion of a control binding."""
import asyncio
import unittest

from tests.campaign.active_writer_pty import InputCompletionProbe

try:
    from textual.app import App
    from textual.widgets import Input, Static, Select
    from textual._xterm_parser import XTermParser
    from cmw.jobs.catalogue_ui import CatalogueSearch
except ImportError:
    App = object


class Probe(InputCompletionProbe, App):
    def compose(self):
        yield Static('Task-owned input-order fixture')

    async def until(self, predicate):
        deadline = asyncio.get_running_loop().time()+2
        while not predicate():
            assert asyncio.get_running_loop().time() < deadline, 'Input condition deadline'
            await asyncio.sleep(.001)

    def native_burst(self, text):
        for event in XTermParser().feed(text):
            self._driver.process_message(event)


@unittest.skipIf(App is object, 'Jobs UI extra unavailable')
class ActiveWriterInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_burst_counterexample_keeps_focus_but_deletes_new_text(self):
        app = Probe()
        async with app.run_test(size=(140,40)) as pilot:
            app.push_screen(CatalogueSearch(None, {}, 'Synthetic no filter'))
            await pilot.pause()
            before = app.input_count()
            app.native_burst('\x01\x0bSynthetic')
            await app.input_received(before, binding='ctrl+k')
            field = app.screen.query_one('#catalogue-text',Input)
            self.assertTrue(field.has_focus)
            self.assertEqual(field.value,'')
            deletion = next(row for row in app.bindings if row['key']=='ctrl+k')
            self.assertEqual(deletion['before']['value'],'Synthetic')
            self.assertEqual(deletion['after']['value'],'')

    async def test_split_bursts_wait_for_binding_completion_before_replacement(self):
        for old in ('', 'Previous search'):
            with self.subTest(original=old):
                app = Probe()
                async with app.run_test(size=(140,40)) as pilot:
                    app.push_screen(CatalogueSearch(None, {}, 'Synthetic no filter'))
                    await pilot.pause()
                    field = app.screen.query_one('#catalogue-text',Input)
                    field.value = old  # Fixture setup only, before the native event sequence.
                    await pilot.pause()
                    before = app.input_count()
                    app.native_burst('\x01\x0b')
                    clear = await app.input_received(before, binding='ctrl+k')
                    self.assertEqual(clear['state']['value'],'')
                    self.assertEqual(clear['state']['cursor_position'],0)
                    app.native_burst('Synthetic')
                    await app.until(lambda: field.value=='Synthetic')
                    self.assertTrue(field.has_focus)
                    await pilot.pause()
                    self.assertEqual(field.value,'Synthetic')

    async def test_owner_menu_one_down_selects_blank_prompt_not_local(self):
        from types import SimpleNamespace
        app = Probe()
        async with app.run_test(size=(140,40)) as pilot:
            app.push_screen(CatalogueSearch(None, {'LOCAL':SimpleNamespace(next_cursor='synthetic')}, 'Synthetic page'))
            await pilot.pause()
            await pilot.click('#catalogue-owner')
            await app.until(lambda: app.select_state()['catalogue-owner']['expanded']
                            and app.select_state()['catalogue-owner']['overlay_focused'])
            await pilot.pause()
            opened = app.select_state()['catalogue-owner']
            self.assertIsNone(opened['highlighted'])
            self.assertEqual(opened['options'],['Owner with another page','LOCAL'])
            app.native_burst('\x1b[B\r')
            await pilot.pause()
            after = app.select_state()['catalogue-owner']
            self.assertEqual(after['highlighted'],0)
            self.assertEqual(after['value'],'<blank>')
            self.assertFalse(after['expanded'])

    async def test_owner_end_binding_then_enter_selects_literal_local(self):
        from types import SimpleNamespace
        app = Probe()
        async with app.run_test(size=(140,40)) as pilot:
            app.push_screen(CatalogueSearch(None, {'LOCAL':SimpleNamespace(next_cursor='synthetic')}, 'Synthetic page'))
            await pilot.pause()
            await pilot.click('#catalogue-owner')
            await app.until(lambda: app.select_state()['catalogue-owner']['expanded']
                            and app.select_state()['catalogue-owner']['overlay_focused'])
            await app.until(lambda: app.select_state()['catalogue-owner']['overlay_focused'])
            before = app.input_count()
            app.native_burst('\x1b[F')
            await app.input_received(before,binding='end')
            owner = app.select_state()['catalogue-owner']
            self.assertEqual(owner['options'][owner['highlighted']],'LOCAL')
            before = app.input_count()
            app.native_burst('\r')
            await app.input_received(before,binding='enter')
            await app.until(lambda: app.screen.query_one('#catalogue-owner',Select).value=='LOCAL')
            await pilot.pause()
            self.assertFalse(app.select_state()['catalogue-owner']['expanded'])
            self.assertEqual(app.select_state()['catalogue-owner']['value'],'LOCAL')
