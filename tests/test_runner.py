import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from cashify_runner import (partition_branch, plan, parse_quote, export, write_json,
                            validate_url, ReviewRequired, UI, collect_urls_interactively,
                            load_urls_from_file)
from unittest.mock import patch, AsyncMock, MagicMock

class _IsolatedCwdMixin:
    """run_batch now writes runs/Batch_Report.* as a side effect — isolate that to a
    throwaway directory so tests never leave artifacts behind in the real project folder."""
    def setUp(self):
        self._orig_cwd=os.getcwd()
        self._tmp=tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()

DATA=json.loads((Path(__file__).parent/'observed_questionnaire.json').read_text())

def schema(initial=3,accessories=1,age=False,fold=False):
    sets=copy.deepcopy(DATA['sets'])
    if fold:
        sets[1]+= ['Outer screen damaged/line/ broken or Spot','No issue with outer screen']
        sets[3]+= ['Hinges stuck/loose or broken','No hinges issues']
    return dict(initial=[f'Question {i}?' for i in range(initial)],groups=DATA['groups'],
        branches=[dict(label=g,options=o,blocks=partition_branch(g,o,{})) for g,o in zip(DATA['groups'],sets)],
        functional=DATA['funcs'],accessories=['Original Box with same IMEI'] if accessories==1 else
        ['Original Charger of Device','Original Box with same IMEI'],ages=DATA['ages'] if age else [])

QUOTE='Selling price :\n₹18,230\nBase Price\n₹18,250\nProcessing Fee\n- ₹99\n₹100\nIMEI Validation Fee\n- ₹20\nTotal Amount\n₹18,131'

class PlannerTests(unittest.TestCase):
    def test_s23_fe_applicability(self):
        p=plan(schema());self.assertEqual(len(p),44)
        self.assertFalse(any(c['id'].startswith('age-') for c in p))
        self.assertEqual(sum(c['id'].startswith('initial-no') for c in p),3)
    def test_s24_fe_applicability(self): self.assertEqual(len(plan(schema(5,1,True))),49)
    def test_s24_applicability(self): self.assertEqual(len(plan(schema(5,2,True))),51)
    def test_fold_applicability(self): self.assertEqual(len(plan(schema(5,1,True,True))),51)
    def test_discoloration_is_isolated(self):
        c=next(c for c in plan(schema()) if c['label']=='Major Discoloration')
        self.assertCountEqual(c['branch'],['No spots on screen','No line(s) on Display','Major Discoloration'])
    def test_hinge_is_isolated(self):
        c=next(c for c in plan(schema(fold=True)) if c['label']=='Hinges stuck/loose or broken')
        self.assertCountEqual(c['branch'],['No defect on side or back panel','Phone not bent','Hinges stuck/loose or broken'])
    def test_controls_are_explicit(self):
        self.assertEqual(sum(c.get('control',False) for c in plan(schema())),3)
    def test_all_accessories_missing_is_independent_case(self):
        s=schema(accessories=2);p=plan(s)
        c=next(c for c in p if c['id']=='accessories-all-missing')
        self.assertEqual(c['missing'],s['accessories'])
    def test_ids_unique(self):
        p=plan(schema(5,2,True,True));self.assertEqual(len(p),len({c['id'] for c in p}))
    def test_unknown_branch_rejected(self):
        with self.assertRaises(ReviewRequired): partition_branch('New condition',['Mystery option'],{})
    def test_override_requires_complete_coverage(self):
        with self.assertRaises(ReviewRequired):
            partition_branch('New',['A','B'],{'branches':{'New':[{'options':['A'],'healthy':'A'}]}})

class QuoteTests(unittest.TestCase):
    def test_exact_amounts_and_fees(self):
        q=parse_quote(QUOTE);self.assertEqual(q['selling'],18230);self.assertEqual(q['total'],18131)
    def test_fee_values_not_hardcoded(self):
        q=parse_quote(QUOTE.replace('₹99','₹49').replace('18,131','18,181'))
        self.assertEqual(q['processing_fee'],49)
    def test_fee_arithmetic_rejected(self):
        with self.assertRaises(ReviewRequired): parse_quote(QUOTE.replace('18,131','18,132'))
    def test_missing_amount_rejected(self):
        with self.assertRaises(ReviewRequired): parse_quote('Selling price: ₹12,000')
    def test_no_offer_is_not_zero(self):
        q=parse_quote('Your device seems to have very little value due to its condition')
        self.assertEqual(q['status'],'no_offer');self.assertIsNone(q['selling']);self.assertIsNone(q['total'])

class StorageTests(unittest.TestCase):
    def make_state(self):
        s=schema();p=plan(s);q=parse_quote(QUOTE)
        results={c['id']:dict(case=c,quote=copy.deepcopy(q),answers=[],observed_at='test',quote_text=QUOTE) for c in p}
        return dict(url='https://www.cashify.in/sell-old-mobile-phone/used-test',variant_urls=['variant-a'],
            variants={'variant-a':dict(model='Test (8 GB/128 GB)',schema=s,plan=p,results=results,
                                      final_control=dict(quote=copy.deepcopy(q)))})
    def test_checkpoint_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            state=self.make_state();export(Path(d),state)
            saved=json.loads((Path(d)/'checkpoint.json').read_text());self.assertEqual(saved['status'],'complete')
            self.assertEqual(saved['verified_cases'],44)
            self.assertTrue((Path(d)/'Cashify_Test_Results.zip').exists())
    def test_missing_variant_is_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            state=self.make_state();state['variant_urls'].append('variant-b');export(Path(d),state)
            self.assertEqual(state['status'],'incomplete')
    def test_missing_case_is_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            state=self.make_state();del state['variants']['variant-a']['results']['functional-0'];export(Path(d),state)
            self.assertEqual(state['status'],'incomplete')
    def test_final_drift_is_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            state=self.make_state();state['variants']['variant-a']['final_control']['quote']['selling']-=1
            export(Path(d),state);self.assertEqual(state['status'],'incomplete')
    def test_saved_healthy_control_drift_cannot_become_complete(self):
        with tempfile.TemporaryDirectory() as d:
            state=self.make_state();state['variants']['variant-a']['results']['physical-control-1']['quote']['selling']-=1
            export(Path(d),state);self.assertEqual(state['status'],'incomplete')
    def test_failure_exports_partial(self):
        with tempfile.TemporaryDirectory() as d:
            state=self.make_state();state['error']='Interrupted';export(Path(d),state)
            self.assertEqual(state['status'],'incomplete')
    def test_atomic_write_replaces_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'checkpoint.json';write_json(p,{'results':[1]});write_json(p,{'results':[1,2]})
            self.assertEqual(json.loads(p.read_text()),{'results':[1,2]});self.assertFalse(p.with_suffix('.json.tmp').exists())
    def test_url_validation(self):
        self.assertTrue(validate_url('https://www.cashify.in/sell-old-mobile-phone/used-test'))
        for u in ['https://evil.example/used-test','http://www.cashify.in/sell-old-mobile-phone/used-test',
                  'https://www.cashify.in/sell/quote/page']:
            with self.assertRaises(ValueError): validate_url(u)

class FakeLocator:
    def __init__(self,page,index=None): self.page,self.index=page,index
    def nth(self,index): return FakeLocator(self.page,index)
    async def click(self):
        if self.page.allow_change:
            c=self.page.cards[self.index];c['selected']=not c['selected']

class FakePage:
    def __init__(self,allow_change=True):
        self.allow_change=allow_change
        self.cards=[dict(index=i,label=x,selected=False) for i,x in enumerate(['A','B'])]
    async def evaluate(self,*args): return dict(cards=copy.deepcopy(self.cards),text='Fixture page',questions=[])
    def locator(self,*args): return FakeLocator(self)

class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_verified_against_actual_state(self):
        ui=UI(FakePage(),{},1);answer=await ui.choose(['A','B'],[1])
        self.assertEqual(answer['selected'],['B']);self.assertTrue(answer['verified'])
    async def test_failed_click_never_claimed_verified(self):
        ui=UI(FakePage(False),{},1)
        with self.assertRaises(ReviewRequired): await ui.choose(['A','B'],[1])
    async def test_changed_options_stop_before_click(self):
        ui=UI(FakePage(),{},1)
        with self.assertRaises(ReviewRequired): await ui.choose(['A','C'],[1])
    async def test_discovery_changes_rejected(self):
        with self.assertRaises(ReviewRequired): UI.learn({'initial':['A?']},'initial',['B?'])

class DiscoveryRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_physical_route_can_omit_functional_checks(self):
        s={'functional':['Front Camera not working','Finger Touch not working']}
        actual=['Front Camera not working']
        self.assertEqual(UI.functional_options(s,actual,{'group':'Broken screen'},True),actual)
        self.assertEqual(len(s['functional']),2)
        self.assertEqual(s['conditional_functional']['Broken screen'],actual)
    def test_new_functional_option_still_requires_review(self):
        with self.assertRaisesRegex(ReviewRequired,'Observed:'):
            UI.functional_options({'functional':['Front Camera not working']},
                                  ['New defect'],{'group':'Broken screen'},True)
    def test_baseline_functional_subset_is_not_silently_accepted(self):
        with self.assertRaises(ReviewRequired):
            UI.functional_options({'functional':['A','B']},['A'],{},True)
    def test_main_sidebar_does_not_classify_active_branch(self):
        s=dict(text='Functional or Physical Problems',panel='Screen Physical Condition',
               cards=[dict(label='Screen cracked/ glass broken')])
        self.assertEqual(UI.kind(s),'branch')
    async def test_heading_before_new_cards_is_not_ready(self):
        ui=UI(FakePage(),{},1)
        s=dict(text='Functional or Physical Problems',cards=[dict(label='Screen cracked/ glass broken')])
        self.assertFalse(ui.ready(s,['Front Camera not working']))
    async def test_wait_discards_transient_option_list(self):
        def snap(options):
            return dict(text='Functional or Physical Problems',questions=[],
                        cards=[dict(label=x,index=i,selected=False) for i,x in enumerate(options)])
        class ChangingUI(UI):
            def __init__(self):
                super().__init__(FakePage(),{'stable_seconds':0},2)
                self.samples=[snap(['Screen cracked/ glass broken']),snap(['Front Camera not working'])]
            async def observe(self):
                if len(self.samples)>1:return self.samples.pop(0)
                return self.samples[0]
        s=await ChangingUI().wait({'functional'},['Front Camera not working'])
        self.assertEqual(s['cards'][0]['label'],'Front Camera not working')

class BatchInputTests(unittest.TestCase):
    def test_numbered_prompts_stop_on_blank_line_and_confirm_runs_batch(self):
        urls=['https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9-plus',
              'https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9','','y']
        prompts=[]
        def fake_input(prompt=''):
            prompts.append(prompt)
            return urls.pop(0)
        with patch('builtins.input', side_effect=fake_input):
            collected=collect_urls_interactively()
        self.assertEqual(collected,[
            'https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9-plus',
            'https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9'])
        self.assertEqual(prompts,['Paste the (1) Cashify device URL: ','Paste the (2) Cashify device URL: ',
                                   'Paste the (3) Cashify device URL: ','Run all 2? [Y/n] '])
    def test_declining_the_confirmation_cancels_the_batch(self):
        urls=['https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9-plus','','n']
        with patch('builtins.input', side_effect=urls.copy()):
            self.assertEqual(collect_urls_interactively(),[])
    def test_duplicate_paste_is_still_queued_but_noted(self):
        dup='https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s10'
        urls=[dup,dup,'','y']
        with patch('builtins.input', side_effect=urls):
            self.assertEqual(collect_urls_interactively(),[dup,dup])
    def test_two_immediate_blank_lines_cancel_with_no_confirmation_prompt(self):
        # A single blank line before any URL is tolerated (see the next test) in case it's
        # a paste artifact; a second one in a row confirms the user really means to cancel.
        with patch('builtins.input', return_value='') as mocked:
            self.assertEqual(collect_urls_interactively(),[])
            self.assertEqual(mocked.call_count,2)

    def test_leading_blank_line_before_a_pasted_url_is_skipped_not_fatal(self):
        # Reproduces pasting a URL with a blank line above it: the terminal delivers that
        # as its own empty Enter press before the real URL line arrives.
        inputs=['', 'https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s25', '', 'y']
        with patch('builtins.input', side_effect=inputs):
            collected=collect_urls_interactively()
        self.assertEqual(collected,['https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s25'])

    def test_stdin_eof_stops_cleanly_instead_of_crashing(self):
        with patch('builtins.input', side_effect=EOFError()):
            self.assertEqual(collect_urls_interactively(),[])

    def test_invisible_characters_are_stripped_from_pasted_urls(self):
        dirty='\ufeff https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s25\u200b '
        inputs=[dirty,'','y']
        with patch('builtins.input', side_effect=inputs):
            collected=collect_urls_interactively()
        self.assertEqual(collected,['https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s25'])

class RunBatchRecoveryTests(_IsolatedCwdMixin, unittest.TestCase):
    def test_relaunches_and_retries_once_after_a_browser_crash(self):
        import cashify_runner as cr
        url='https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9'
        calls=dict(launch=0, process=0)

        async def fake_process_device(context, u, args):
            calls['process']+=1
            if calls['process']==1:
                raise RuntimeError('Protocol error (Target.createTarget): Failed to open a new tab')
            return Path('/fake/out')

        fake_context=MagicMock()
        fake_context.pages=[]
        fake_context.close=AsyncMock()

        async def fake_launch_persistent_context(*a, **kw):
            calls['launch']+=1
            return fake_context
        fake_chromium=MagicMock()
        fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium

        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False

        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            summary=asyncio.run(cr.run_batch([url], args))

        self.assertEqual(calls, dict(launch=2, process=2))  # initial launch + one relaunch/retry
        self.assertEqual(summary, [dict(url=url, status='done', out='/fake/out')])

    def test_gives_up_after_one_failed_retry_but_keeps_going(self):
        import cashify_runner as cr
        url='https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s9'
        calls=dict(launch=0, process=0)

        async def fake_process_device(context, u, args):
            calls['process']+=1
            raise RuntimeError('Protocol error (Target.createTarget): Failed to open a new tab')

        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw):
            calls['launch']+=1
            return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium

        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False

        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            summary=asyncio.run(cr.run_batch([url], args))

        self.assertEqual(calls, dict(launch=2, process=2))  # tried once, relaunched, tried again, then gave up
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]['url'], url)
        self.assertTrue(summary[0]['status'].startswith('RuntimeError'))
        self.assertIsNone(summary[0]['out'])

class RunBatchBadUrlTests(_IsolatedCwdMixin, unittest.TestCase):
    def test_one_malformed_url_does_not_abort_the_whole_batch(self):
        import cashify_runner as cr
        good='https://www.cashify.in/sell-old-mobile-phone/used-oneplus-11r'
        bad='http://cashify.in/sell-old-mobile-phone/used-oneplus-nord-ce-5g'  # wrong scheme/host
        calls=dict(launch=0, process=0)

        async def fake_process_device(context, u, args):
            calls['process']+=1
            validate_url(u)  # same check process_device itself performs first
            return Path(f'/fake/{calls["process"]}')

        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw):
            calls['launch']+=1
            return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium

        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False

        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            summary=asyncio.run(cr.run_batch([good, bad], args))

        self.assertEqual(calls['launch'], 1)  # no pointless relaunch for a bad URL
        self.assertEqual(calls['process'], 1)  # the bad URL is skipped before process_device is ever called
        self.assertEqual(summary[0], dict(url=good, status='done', out='/fake/1'))
        self.assertEqual(summary[1]['url'], bad)
        self.assertTrue(summary[1]['status'].startswith('ValueError'))
        self.assertIsNone(summary[1]['out'])

    def test_unicode_decode_error_is_not_mistaken_for_a_bad_url(self):
        # UnicodeDecodeError IS a ValueError subclass — a real bug that raises one while
        # reading a saved file must still go through the normal browser-relaunch retry
        # path, not get mislabeled and skipped as if the URL itself were malformed.
        import cashify_runner as cr
        url='https://www.cashify.in/sell-old-mobile-phone/used-samsung-galaxy-s25-plus'
        calls=dict(launch=0, process=0)

        async def fake_process_device(context, u, args):
            calls['process']+=1
            if calls['process']==1:
                raise UnicodeDecodeError('charmap', b'\x9d', 0, 1, 'character maps to <undefined>')
            return Path('/fake/out')

        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw):
            calls['launch']+=1
            return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium

        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False

        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            summary=asyncio.run(cr.run_batch([url], args))

        self.assertEqual(calls, dict(launch=2, process=2))  # relaunched and retried, as for any other crash
        self.assertEqual(summary, [dict(url=url, status='done', out='/fake/out')])

class DroppedFileTests(unittest.TestCase):
    def _write(self, tmp, lines):
        p=Path(tmp)/'urls.txt'
        p.write_text('\n'.join(lines), encoding='utf-8')
        return p

    def test_dropping_a_plain_path_expands_every_url_in_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls=['https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-note-6-pro',
                  '# a comment line, ignored',
                  '',
                  'https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a2']
            path=self._write(tmp, urls)
            inputs=[str(path), '', 'y']
            with patch('builtins.input', side_effect=inputs):
                collected=collect_urls_interactively()
        self.assertEqual(collected,[
            'https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-note-6-pro',
            'https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a2'])

    def test_dropping_a_quoted_windows_path_also_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self._write(tmp, ['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5'])
            inputs=[f'"{path}"', '', 'y']
            with patch('builtins.input', side_effect=inputs):
                collected=collect_urls_interactively()
        self.assertEqual(collected,['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5'])

    def test_can_mix_a_dropped_file_with_individually_pasted_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self._write(tmp, ['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5'])
            inputs=['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5a',
                    str(path),
                    'https://www.cashify.in/sell-old-mobile-phone/used-redmi-y1',
                    '', 'y']
            with patch('builtins.input', side_effect=inputs):
                collected=collect_urls_interactively()
        self.assertEqual(collected,[
            'https://www.cashify.in/sell-old-mobile-phone/used-redmi-5a',
            'https://www.cashify.in/sell-old-mobile-phone/used-redmi-5',
            'https://www.cashify.in/sell-old-mobile-phone/used-redmi-y1'])

    def test_a_url_already_loaded_from_a_file_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self._write(tmp, ['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5'])
            inputs=['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5', str(path), '', 'y']
            with patch('builtins.input', side_effect=inputs):
                collected=collect_urls_interactively()
        self.assertEqual(collected,['https://www.cashify.in/sell-old-mobile-phone/used-redmi-5'])

    def test_nonexistent_path_with_no_url_in_it_is_rejected_not_silently_queued(self):
        # A string that is neither a URL nor a path to a real file is rejected right
        # away with a clear reason — not silently queued as a bogus "URL" that could
        # only ever fail later. Two blanks after it (nothing queued yet) cancel cleanly.
        inputs=['/no/such/file.txt', '', '']
        with patch('builtins.input', side_effect=inputs):
            collected=collect_urls_interactively()
        self.assertEqual(collected,[])

    def test_a_url_extracted_from_a_noisy_pasted_line_is_queued_correctly(self):
        # Reproduces pasting a line copied straight from this script's OWN confirmation
        # screen ("12. https://...") instead of a clean URL.
        inputs=['12. https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-7a', '', 'y']
        with patch('builtins.input', side_effect=inputs):
            collected=collect_urls_interactively()
        self.assertEqual(collected,['https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-7a'])

    def test_pure_noise_line_with_no_url_at_all_is_rejected(self):
        # Reproduces lines like "27 URL(s) queued:" or "Run all 27? [Y/n] Y" ending up
        # in a file by accident (e.g. the file turned out to be a copy of this script's
        # own terminal output rather than a clean URL list) — these have no URL in them
        # at all and must be rejected, not queued as something doomed to fail later.
        inputs=['27 URL(s) queued:', 'Run all 27? [Y/n] Y', '', '']
        with patch('builtins.input', side_effect=inputs):
            collected=collect_urls_interactively()
        self.assertEqual(collected,[])

    def test_load_urls_from_file_extracts_urls_from_noisy_lines_and_drops_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'urls.txt'
            path.write_text('\n'.join([
                '27 URL(s) queued:',
                '1. https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-7a',
                '2. https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a3',
                'Run all 27? [Y/n] Y',
            ]), encoding='utf-8')
            self.assertEqual(load_urls_from_file(path),[
                'https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-7a',
                'https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a3'])

    def test_load_urls_from_file_matches_the_uploaded_xiaomi_list_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines=['https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-note-6-pro',
                   'https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a2']
            path=self._write(tmp, lines)
            self.assertEqual(load_urls_from_file(path), lines)

class BatchReportTests(_IsolatedCwdMixin, unittest.TestCase):
    def test_full_and_partial_and_failed_devices_are_classified_correctly(self):
        from cashify_runner import write_batch_report, device_out_dir
        good_url='https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-note-6-pro'
        partial_url='https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-mi-a2'
        failed_url='https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-6'
        invalid_url='http://cashify.in/sell-old-mobile-phone/used-oneplus-nord-ce-5g'
        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)

        write_json(device_out_dir(good_url,args)/'checkpoint.json',
                   dict(status='complete', verified_cases=10, planned_cases=10))
        write_json(device_out_dir(partial_url,args)/'checkpoint.json',
                   dict(status='incomplete', verified_cases=4, planned_cases=10,
                        error='SiteBlocked: too many requests'))
        write_json(device_out_dir(failed_url,args)/'checkpoint.json',
                   dict(status='incomplete', verified_cases=0, planned_cases=0,
                        error='RuntimeError: Failed to open a new tab'))
        # invalid_url gets no checkpoint at all — never reached a browser

        memory_summary=[dict(url=good_url,status='done',out=str(device_out_dir(good_url,args))),
                        dict(url=partial_url,status='SiteBlocked: too many requests',out=None),
                        dict(url=failed_url,status='RuntimeError: Failed to open a new tab',out=None),
                        dict(url=invalid_url,status='ValueError: Use an https://www.cashify.in device URL.',out=None)]
        text,report_dir=write_batch_report([good_url,partial_url,failed_url,invalid_url],args,memory_summary)

        rows={r['url']:r for r in json.loads((report_dir/'Batch_Report.json').read_text(encoding='utf-8'))}
        self.assertEqual(rows[good_url]['category'],'100% Successful')
        self.assertEqual(rows[partial_url]['category'],'Partially Successful')
        self.assertEqual(rows[partial_url]['verified'],4)
        self.assertEqual(rows[partial_url]['planned'],10)
        self.assertEqual(rows[failed_url]['category'],'Failed')
        self.assertEqual(rows[invalid_url]['category'],'Failed')
        self.assertIn('Use an https://www.cashify.in device URL.',rows[invalid_url]['note'])
        self.assertTrue((report_dir/'Batch_Report.txt').exists())
        self.assertTrue((report_dir/'Batch_Report.csv').exists())
        for u in (good_url,partial_url,failed_url,invalid_url):
            self.assertIn(u,text)  # every URL appears in the human-readable report too

    def test_device_never_reached_gets_its_own_reason(self):
        from cashify_runner import write_batch_report
        never_reached='https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-6a'
        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        text,report_dir=write_batch_report([never_reached],args,[])  # empty: batch stopped before this URL
        rows=json.loads((report_dir/'Batch_Report.json').read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['category'],'Failed')
        self.assertIn('Not reached',rows[0]['note'])

    def test_run_batch_writes_a_report_even_when_interrupted_mid_device(self):
        import cashify_runner as cr
        url='https://www.cashify.in/sell-old-mobile-phone/used-xiaomi-redmi-7'
        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw): return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium
        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False
        async def fake_process_device(context, u, args):
            raise KeyboardInterrupt()
        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(cr.run_batch([url], args))
        report=Path('runs')/'Batch_Report.txt'
        self.assertTrue(report.exists())  # written before the KeyboardInterrupt was allowed to propagate
        self.assertIn(url, report.read_text(encoding='utf-8'))

class RecycleAndDelayTests(_IsolatedCwdMixin, unittest.TestCase):
    def test_recycles_the_browser_every_n_devices_without_a_crash(self):
        import cashify_runner as cr
        urls=[f'https://www.cashify.in/sell-old-mobile-phone/used-device-{i}' for i in range(1,5)]
        calls=dict(launch=0, process=0)
        async def fake_process_device(context, u, args):
            calls['process']+=1
            return Path(f'/fake/{calls["process"]}')
        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw):
            calls['launch']+=1
            return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium
        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False
        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=2)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            summary=asyncio.run(cr.run_batch(urls, args))
        self.assertEqual(calls['process'], 4)
        # Initial launch + a recycle after device 2 (i%2==0, i<len(urls)); no recycle
        # after device 4 since it's the last one — nothing left to use a fresh browser for.
        self.assertEqual(calls['launch'], 2)
        self.assertEqual([r['status'] for r in summary], ['done']*4)

    def test_recycle_every_zero_disables_proactive_recycling(self):
        import cashify_runner as cr
        urls=[f'https://www.cashify.in/sell-old-mobile-phone/used-device-{i}' for i in range(1,5)]
        calls=dict(launch=0)
        async def fake_process_device(context, u, args):
            return Path('/fake/out')
        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw):
            calls['launch']+=1
            return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium
        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False
        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=0, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()):
            asyncio.run(cr.run_batch(urls, args))
        self.assertEqual(calls['launch'], 1)  # just the initial launch, never recycled

    def test_delay_is_slept_between_devices_but_not_after_the_last_one(self):
        import cashify_runner as cr
        urls=[f'https://www.cashify.in/sell-old-mobile-phone/used-device-{i}' for i in range(1,4)]
        async def fake_process_device(context, u, args):
            return Path('/fake/out')
        fake_context=MagicMock(); fake_context.pages=[]; fake_context.close=AsyncMock()
        async def fake_launch_persistent_context(*a, **kw): return fake_context
        fake_chromium=MagicMock(); fake_chromium.launch_persistent_context=fake_launch_persistent_context
        fake_pw=MagicMock(); fake_pw.chromium=fake_chromium
        class FakeAsyncPlaywrightCM:
            async def __aenter__(self_inner): return fake_pw
            async def __aexit__(self_inner, *exc): return False
        sleeps=[]
        async def fake_sleep(seconds): sleeps.append(seconds)
        args=SimpleNamespace(profile='.cashify-browser', timeout=60, output=None, delay=2.5, recycle_every=0)
        with patch('cashify_runner.process_device', side_effect=fake_process_device), \
             patch('playwright.async_api.async_playwright', return_value=FakeAsyncPlaywrightCM()), \
             patch('asyncio.sleep', side_effect=fake_sleep):
            asyncio.run(cr.run_batch(urls, args))
        self.assertEqual(sleeps, [2.5, 2.5])  # between device 1->2 and 2->3, not after device 3

if __name__=='__main__': unittest.main()
