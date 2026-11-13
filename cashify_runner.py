"""Cashify individual-defect audit. Uses visible UI; never books a sale."""
from __future__ import annotations
import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from zipfile import ZipFile, ZIP_DEFLATED

VERSION = '1.4.1'
NO_OFFER = 'Your device seems to have very little value due to its condition'
CARDS = '#calcDrawerLeft .cursor-pointer.border'

class ReviewRequired(RuntimeError): pass
class SiteBlocked(RuntimeError): pass

def norm(value):
    return re.sub(r'\s+', ' ', value).strip()

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def validate_url(url):
    url = clean_pasted_line(url)
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname not in ('www.cashify.in', 'cashify.in'):
        raise ValueError('Use an https://www.cashify.in device URL.')
    if not p.path.startswith('/sell-old-mobile-phone/used-'):
        raise ValueError('Use a device sell-page URL, not a quote or calculator URL.')
    return url

def parse_quote(text):
    if NO_OFFER.lower() in text.lower():
        return dict(status='no_offer', selling=None, base=None, imei_fee=None,
                    processing_fee=None, total=None, reason=NO_OFFER)
    def money(label):
        match = re.search(re.escape(label) + r'\s*:?\s*[-−]?\s*₹\s*([\d,]+)', text, re.I)
        if not match:
            raise ReviewRequired('Missing quote amount: ' + label)
        return int(match[1].replace(',', ''))
    q = dict(status='quoted', selling=money('Selling price'), base=money('Base Price'),
             imei_fee=money('IMEI Validation Fee'), processing_fee=money('Processing Fee'),
             total=money('Total Amount'))
    if q['base'] - q['imei_fee'] != q['selling']:
        raise ReviewRequired('Base / IMEI fee arithmetic does not match.')
    if q['selling'] - q['processing_fee'] != q['total']:
        raise ReviewRequired('Selling / processing fee arithmetic does not match.')
    return q

def partition_branch(label, options, rules):
    """Each block is one mutually exclusive subquestion; healthy=None for screen scratches."""
    custom = rules.get('branches', {}).get(label)
    if custom is not None:
        blocks = custom
    elif options == ['Screen cracked/ glass broken', 'Chipped/cracked outside display area',
                     'More than 2 scratches on screen', '1-2 scratches on screen']:
        blocks = [dict(options=options, healthy=None)]
    else:
        # Verified site subquestions end with these explicit healthy choices.
        healthy = {'No spots on screen', 'No line(s) on Display', 'No Discoloration',
                   'No scratches', 'No dents', 'No defect on side or back panel',
                   'Phone not bent', 'No hinges issues', 'No issue with outer screen'}
        blocks, pending = [], []
        for option in options:
            pending.append(option)
            if option in healthy:
                blocks.append(dict(options=pending, healthy=option))
                pending = []
        if pending or not blocks:
            raise ReviewRequired(f'Unknown branch: {label}. Add its exact blocks to rules.json.')
    if [o for block in blocks for o in block['options']] != options:
        raise ReviewRequired('Branch blocks must cover every observed option once, in order: ' + label)
    if any(b['healthy'] is not None and b['healthy'] not in b['options'] for b in blocks):
        raise ReviewRequired('Invalid healthy option in branch rules: ' + label)
    return blocks

def plan(schema):
    tests = [dict(id='baseline', label='Flawless baseline')]
    for i, option in enumerate(schema['functional']):
        tests.append(dict(id=f'functional-{i}', label=option, functional=option))
    for g, branch in enumerate(schema['branches']):
        blocks = branch['blocks']
        neutral = [b['healthy'] for b in blocks if b['healthy'] is not None]
        for b, block in enumerate(blocks):
            for j, option in enumerate(block['options']):
                if option == block['healthy']:
                    continue
                selected = [x for x in neutral if x != block['healthy']] + [option]
                tests.append(dict(id=f'physical-{g}-{b}-{j}', label=option,
                                  group=branch['label'], branch=selected))
        if len(neutral) == len(blocks):
            tests.append(dict(id=f'physical-control-{g}', label=branch['label'] + ': no defects',
                              group=branch['label'], branch=neutral, control=True))
    for i, question in enumerate(schema['initial']):
        tests.append(dict(id=f'initial-no-{i}', label=question + ' — No', initial_no=i))
    for i, option in enumerate(schema['accessories']):
        tests.append(dict(id=f'accessory-{i}', label=option + ' missing', missing=[option]))
    # Same coverage convention as the manual reports: individual accessories + all absent.
    if len(schema['accessories']) > 1:
        tests.append(dict(id='accessories-all-missing', label='All offered accessories missing',
                          missing=schema['accessories']))
    for i, age in enumerate(schema['ages'][1:], 1):
        tests.append(dict(id=f'age-{i}', label=age, age=age))
    return tests

class UI:
    def __init__(self, page, rules, timeout):
        self.page, self.rules, self.timeout = page, rules, timeout

    async def snapshot(self):
        # Read rendered DOM only. No private APIs, app state, or pricing endpoints.
        return await self.page.evaluate('''selector => {
          const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
          const all = [...document.querySelectorAll(selector)];
          const cards = all.map((e,index)=>({index,label:e.innerText.trim(),
            selected:/border-(primary|error)(?:\\s|$)/.test(e.className),visible:visible(e)}))
            .filter(x=>x.visible && x.label);
          const questions = all.filter(e=>visible(e) && e.innerText.trim()==='Yes').map(e=>{
            let p=e.parentElement;
            while(p && !(p.innerText.includes('?') && /\\bNo\\b/.test(p.innerText))) p=p.parentElement;
            return p ? p.innerText.split('\\n').map(x=>x.trim()).find(x=>x.endsWith('?')) : null;
          });
          return {text:document.querySelector('main')?.innerText || document.body.innerText,
                  panel:document.querySelector('#calcDrawerLeft')?.innerText || '',
                  cards,questions};
        }''', self.rules.get('card_selector', CARDS))

    async def observe(self):
        s = await self.snapshot()
        text = s['text']
        if re.search(r'verify you are human|checking your browser|unusual traffic|automated traffic|access denied', text, re.I):
            raise SiteBlocked('This browser was challenged or blocked. Checkpoint saved; no bypass attempted.')
        return s

    @staticmethod
    def kind(s):
        full = s['text'].lower()
        if NO_OFFER.lower() in full or ('selling price' in full and 'total amount' in full) or 'exact value' in full or 'get paid' in full: return 'quote'
        t = (s.get('panel') or s['text']).lower()
        if 'what is your mobile age?' in t: return 'age'
        if 'functional or physical problems' in t: return 'functional'
        if 'please select accessories which are available' in t: return 'accessories'
        if 'select screen/body defects' in t: return 'groups'
        if 'tell us more about your device?' in t and s['cards']: return 'initial'
        if s['cards']: return 'branch'
        return 'loading'

    def ready(self, s, expected=None):
        labels = [c['label'] for c in s['cards']]
        kind = self.kind(s)
        if kind == 'quote': return True
        if not labels: return False
        if kind == 'initial':
            return (len(labels)%2 == 0 and labels == ['Yes','No']*(len(labels)//2)
                    and len(s['questions']) == len(labels)//2 and all(s['questions']))
        if kind == 'functional':
            # Reject old branch/accessory cards while React replaces the new heading/cards.
            known = expected or []
            if known and set(labels).issubset(known): return True
            return all(re.search(r'not working|faulty|is not working|camera glass broken', x, re.I)
                       for x in labels)
        return True

    async def wait(self, kinds, expected=None):
        deadline = time.monotonic() + self.timeout
        previous, since = None, None
        while time.monotonic() < deadline:
            s = await self.observe()
            signature = (self.kind(s), tuple(c['label'] for c in s['cards']), tuple(s['questions']))
            if self.kind(s) in kinds and self.ready(s, expected):
                if signature != previous:
                    previous, since = signature, time.monotonic()
                elif time.monotonic()-since >= self.rules.get('stable_seconds', .75):
                    return s
            else: previous, since = None, None
            await asyncio.sleep(.25)
        raise ReviewRequired('Page did not stabilize at ' + '/'.join(kinds) + '. Observed options: '
                             + repr([c['label'] for c in s['cards']]) + '. URL: ' + self.page.url)

    async def choose(self, expected, selected, s=None):
        s = s or await self.observe()
        if [x['label'] for x in s['cards']] != expected:
            raise ReviewRequired('Options changed. Observed: ' + repr([x['label'] for x in s['cards']]))
        if not set(selected).issubset(set(range(len(expected)))):
            raise ReviewRequired('Selection index outside observed options.')
        for i, card in enumerate(s['cards']):
            if card['selected'] != (i in selected):
                await self.page.locator(self.rules.get('card_selector', CARDS)).nth(card['index']).click()
        after = await self.observe()
        if [x['label'] for x in after['cards']] != expected:
            raise ReviewRequired('Page changed before selection verification.')
        if [i for i,x in enumerate(after['cards']) if x['selected']] != sorted(selected):
            raise ReviewRequired('Selected-card styles did not match requested answers.')
        return dict(options=expected, selected=[expected[i] for i in selected], verified=True)

    async def cont(self):
        # The only button the questionnaire runner submits. No Sell Now, order, address or payment action.
        await self.page.get_by_role('button', name='Continue', exact=True).click()

    @staticmethod
    def learn(schema, key, value):
        if schema.get(key) and schema[key] != value:
            raise ReviewRequired('Questionnaire changed during discovery: ' + key
                                 + '\nExpected: ' + repr(schema[key]) + '\nObserved: ' + repr(value))
        schema[key] = value

    @staticmethod
    def functional_options(schema, options, case, discover):
        baseline = schema.get('functional', [])
        if not baseline:
            if not discover: raise ReviewRequired('Missing baseline functional options.')
            schema['functional'] = options
        elif options != baseline:
            # Some physical conditions hide checks already covered by that condition.
            # Only observed subsets are accepted, on a physical branch, with every option logged.
            if not case.get('group') or not options or not set(options).issubset(baseline):
                raise ReviewRequired('Functional options differ from baseline.\nExpected: '
                                     + repr(baseline) + '\nObserved: ' + repr(options))
            schema.setdefault('conditional_functional', {})[case['group']] = options
        if case.get('functional') and case['functional'] not in options:
            raise ReviewRequired('Requested functional option is not offered on this route.')
        return options

    async def run_case(self, variant, schema, case, discover=False):
        print(f"  Testing case: {case.get('label', case.get('id', 'unknown'))}", flush=True)
        print(f"    [Browser] Navigating to start questionnaire: {variant['start']}", flush=True)
        await self.page.goto(variant['start'], wait_until='domcontentloaded')
        s = await self.wait({'initial'})
        if variant['model'] not in s['text']:
            raise ReviewRequired('Model/variant mismatch at questionnaire start.')
        labels = [x['label'] for x in s['cards']]
        if len(labels)%2 or labels != ['Yes','No'] * (len(labels)//2):
            raise ReviewRequired('Opening questions are not a recognized Yes/No layout.')
        questions = s['questions']
        if len(questions) != len(labels)//2 or any(q is None for q in questions):
            raise ReviewRequired('Cannot associate every opening answer with a question.')
        if discover: self.learn(schema, 'initial', questions)
        elif questions != schema['initial']: raise ReviewRequired('Opening questions changed.')
        answer_log = []
        initial = await self.choose(labels, [2*i + int(case.get('initial_no')==i) for i in range(len(questions))], s)
        initial['questions'] = questions
        initial['responses'] = [dict(question=q, answer='No' if case.get('initial_no')==i else 'Yes')
                                for i,q in enumerate(questions)]
        answer_log.append(dict(stage='initial', **initial))
        print("    [Browser] Submitting initial questions...", flush=True)
        await self.cont()
        s = await self.wait({'groups'})
        if discover: self.learn(schema, 'groups', [x['label'] for x in s['cards']])
        group = case.get('group')
        selected = [] if group is None else [schema['groups'].index(group)]
        answer_log.append(dict(stage='groups', **await self.choose(schema['groups'], selected, s)))
        print(f"    [Browser] Selected physical group: {group or 'None'}", flush=True)
        await self.cont()
        if group is not None:
            s = await self.wait({'branch'})
            options = [x['label'] for x in s['cards']]
            if discover:
                branch = dict(label=group, options=options, blocks=partition_branch(group, options, self.rules))
                schema['branches'].append(branch)
                # Screen-only damage has no healthy option; the exploration quote is never a baseline/result.
                desired = [b['healthy'] if b['healthy'] is not None else b['options'][-1] for b in branch['blocks']]
            else:
                branch = next(b for b in schema['branches'] if b['label']==group)
                desired = case['branch']
            answer_log.append(dict(stage='branch', group=group,
                                   **await self.choose(branch['options'], [branch['options'].index(x) for x in desired], s)))
            print(f"    [Browser] Selected branch options: {desired}", flush=True)
            await self.cont()
        s = await self.wait({'functional'}, schema.get('functional'))
        functional = self.functional_options(schema, [x['label'] for x in s['cards']], case, discover)
        desired = [] if not case.get('functional') else [functional.index(case['functional'])]
        answer_log.append(dict(stage='functional', **await self.choose(functional, desired, s)))
        print(f"    [Browser] Functional defect selected: {case.get('functional') or 'None'}", flush=True)
        await self.cont()
        s = await self.wait({'accessories'})
        if discover: self.learn(schema, 'accessories', [x['label'] for x in s['cards']])
        desired = [i for i,x in enumerate(schema['accessories']) if x not in case.get('missing',[])]
        answer_log.append(dict(stage='accessories', **await self.choose(schema['accessories'], desired, s)))
        print(f"    [Browser] Accessories state processed (missing: {case.get('missing', [])})", flush=True)
        await self.cont()
        s = await self.wait({'age','quote'})
        if self.kind(s) == 'age':
            raw_ages = [x['label'] for x in s['cards']]
            ages = [x.split('\n')[0].strip() for x in raw_ages]
            if discover: self.learn(schema, 'ages', ages)
            if ages != schema['ages']: raise ReviewRequired('Age bands changed.')
            wanted = case.get('age', ages[0])
            # These cards submit immediately on click or have a Continue button
            index = ages.index(wanted)
            print(f"    [Browser] Selecting age band: {wanted}", flush=True)
            card_locator = self.page.locator(self.rules.get('card_selector',CARDS)).nth(s['cards'][index]['index'])
            await card_locator.click()
            await asyncio.sleep(0.5)
            # If Cashify requires clicking Continue after age selection
            cont_btn = self.page.get_by_role('button', name='Continue', exact=True)
            if await cont_btn.count() and await cont_btn.is_visible():
                await cont_btn.click()
            answer_log.append(dict(stage='age', options=ages, selected=[wanted], verified=True,
                                   verification='Exact observed card clicked; quote transition verified.'))
            s = await self.wait({'quote'})
        else:
            answer_log.append(dict(stage='age', options=schema['ages'], selected=[], asked=False))
        if variant['model'] not in s['text']:
            raise ReviewRequired('Model/variant mismatch at quote.')
        quote = parse_quote(s['text'])
        return dict(case=case, quote=quote, answers=answer_log, observed_at=utcnow(), quote_text=s['text'])

async def variants(page, url, timeout):
    print(f"Opening model page: {url}", flush=True)
    await page.goto(url, wait_until='domcontentloaded')
    await page.get_by_role('heading',level=1).wait_for(timeout=timeout*1000)
    # If a variant URL was pasted, its breadcrumb exposes the model-level page.
    if not await page.get_by_text('Choose a variant', exact=True).count():
        breadcrumbs = await page.locator('[aria-label="breadcrumb"] a').evaluate_all(
            '(nodes)=>nodes.map(a=>({href:a.href,text:a.innerText}))')
        parent = next((a['href'] for a in reversed(breadcrumbs)
                       if '/sell-old-mobile-phone/used-' in a['href'] and a['href']!=page.url), None)
        if parent:
            print(f"Navigating to parent model page: {parent}", flush=True)
            await page.goto(parent, wait_until='domcontentloaded')
            await page.get_by_role('heading',level=1).wait_for()
    if await page.get_by_text('Choose a variant', exact=True).count():
        links = await page.locator('a[href]').evaluate_all(
            '(nodes)=>nodes.map(a=>({url:a.href,label:a.innerText.trim()}))')
        links = [a for a in links if re.fullmatch(r'(?:\d+\s*GB\s*/\s*)?\d+\s*(?:GB|TB)', a['label'],re.I)
                 and '/sell-old-mobile-phone/used-' in a['url']]
        if not links: raise ReviewRequired('Variant list found but no recognized variant links.')
        found = list({a['url']:a for a in links}.values())
        print(f"Discovered {len(found)} storage/RAM variant(s).", flush=True)
        return found
    return [dict(url=page.url,label=(await page.get_by_role('heading',level=1).inner_text()))]

async def calculator(page, variant_url, timeout):
    print(f"Transitioning variant to calculator: {variant_url}", flush=True)
    await page.goto(variant_url, wait_until='domcontentloaded')
    button = page.get_by_role('button', name='Get Exact Value', exact=True)
    if await button.count():
        await button.click()
    
    # Check if a login or mobile number prompt appears
    try:
        await page.wait_for_url('**/sell/calculator/**', timeout=8000)
    except Exception:
        text = await page.locator('body').inner_text()
        if re.search(r'otp|mobile number|log.?in|enter mobile', text, re.I):
            print("\n" + "="*70, flush=True)
            print(">>> ACTION REQUIRED: Cashify Mobile & OTP Login Prompt Detected!", flush=True)
            print(">>> Please type your mobile number and OTP directly in the browser.", flush=True)
            print(">>> Waiting for login completion...", flush=True)
            print("="*70 + "\n", flush=True)
            
            # Wait up to 300 seconds for user to enter number and OTP and reach calculator
            await page.wait_for_url('**/sell/calculator/**', timeout=300000)
            print(">>> Login successful! Continuing automated inspection...", flush=True)
        else:
            await page.wait_for_url('**/sell/calculator/**', timeout=timeout*1000)
            
    model = parse_qs(urlparse(page.url).query).get('pn',[None])[0]
    if not model: raise ReviewRequired('Calculator URL has no explicit model/variant identity.')
    print(f"Successfully loaded calculator for model: {model}", flush=True)
    return dict(url=variant_url,start=page.url,model=model)

def export(out, state):
    summaries, full = [], []
    all_complete = (bool(state.get('variants')) and not state.get('error')
                    and set(state.get('variants',{})) == set(state.get('variant_urls',[])))
    done = expected = 0
    for url,v in state.get('variants',{}).items():
        tests = v.get('plan',[])
        results = v.get('results',{})
        base = results.get('baseline',{}).get('quote',{})
        control = v.get('final_control',{}).get('quote')
        keys = ['status','base','selling','imei_fee','processing_fee','total']
        matched = bool(control) and all(control.get(k)==base.get(k) for k in keys)
        healthy_match = all(all(results[c['id']]['quote'].get(k)==base.get(k) for k in keys)
                            for c in tests if c.get('control') and c['id'] in results)
        complete = bool(tests) and all(c['id'] in results for c in tests) and matched and healthy_match
        v['complete'], v['final_control_matches'] = complete, matched
        v['healthy_controls_match'] = healthy_match
        all_complete &= complete
        done += len(results); expected += len(tests)
        full.append('\nMODEL: '+v['model']+'\nSource: '+url)
        for case in tests:
            row = results.get(case['id'])
            summary = dict(model=v['model'],case_id=case['id'],test=case['label'],
                           status='pending',selling=None,base=None,imei_fee=None,processing_fee=None,
                           total=None,deduction=None,total_reduction=None,observed_at=None)
            if row:
                q=row['quote'];summary.update(q,observed_at=row['observed_at'])
                if q['status']=='quoted' and base.get('status')=='quoted':
                    summary.update(deduction=base['selling']-q['selling'],total_reduction=base['total']-q['total'])
                full.append('\nCASE '+case['id']+': '+case['label']+'\n'+row['observed_at'])
                full.append(json.dumps(row['answers'],ensure_ascii=False,indent=2))
                # Include absent branches explicitly, so unasked questions are never implied answered.
                asked=case.get('group')
                for branch in v.get('schema',{}).get('branches',[]):
                    if branch['label']!=asked:
                        full.append(branch['label']+' — NOT ASKED: '+json.dumps(branch['options'],ensure_ascii=False))
                full.append(json.dumps(summary,ensure_ascii=False,indent=2))
            summaries.append(summary)
        full.append('Final flawless recheck matches: '+str(matched))
    state['status']='complete' if all_complete else 'incomplete'
    state['verified_cases'],state['planned_cases']=done,expected
    write_json(out/'checkpoint.json',state)
    write_json(out/'results.json',summaries)
    fields=['model','case_id','test','status','base','selling','deduction','imei_fee','processing_fee','total','total_reduction','observed_at']
    with (out/'results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(summaries)
    header=(f"Cashify individual-defect results — {state['status'].upper()}\n{done}/{expected} planned cases verified\n"
            'Deduction = initial flawless Selling price minus case Selling price.\n'
            'No-offer monetary values are unavailable, not zero. Final inspection can change prices.\n'
            'Every selected answer is recorded; unvisited branches are marked NOT ASKED.\n'
            'This audits each individual defect, neutral branch controls and offered accessory/age states.\n'
            'It does not exhaust all combinations of multiple defects. No sale is booked.\n')
    (out/'full_test_results.txt').write_text(header+'\n'.join(full),encoding='utf-8')
    with ZipFile(out/'Cashify_Test_Results.zip','w',compression=ZIP_DEFLATED) as z:
        for name in ['checkpoint.json','results.json','results.csv','full_test_results.txt']:
            z.write(out/name,name)
        if state.get('error') and (out/'diagnostic.json').exists():
            z.write(out/'diagnostic.json','diagnostic.json')

def device_slug(url):
    return urlparse(url).path.rsplit('/',1)[-1]

def device_out_dir(url, args):
    """The exact output folder a device's results live/will live in — shared by
    process_device and the batch report below so they can never disagree about where
    to look. --output is only ever allowed for a single-URL run (main() enforces that),
    so this formula is safe to use for every device regardless of batch size."""
    return Path(args.output or Path('runs')/device_slug(url)).resolve()

def classify_device(out_dir):
    """Read a device's own checkpoint.json straight off disk (not from in-memory state)
    so a device that was still mid-run when the process was interrupted or killed is
    reported using whatever progress it actually managed to save, not guessed at."""
    checkpoint=Path(out_dir)/'checkpoint.json'
    if not checkpoint.exists():
        return dict(category='Failed',verified=0,planned=0,note='Not started')
    try:
        state=json.loads(checkpoint.read_text(encoding='utf-8'))
    except Exception as e:
        return dict(category='Failed',verified=0,planned=0,note=f'checkpoint.json unreadable: {e}')
    verified=state.get('verified_cases',0);planned=state.get('planned_cases',0)
    error=state.get('error')
    if state.get('status')=='complete' and not error:
        return dict(category='100% Successful',verified=verified,planned=planned,note='')
    if verified>0:
        return dict(category='Partially Successful',verified=verified,planned=planned,
                    note=error or 'Stopped before every planned case was verified')
    return dict(category='Failed',verified=verified,planned=planned,note=error or 'No cases verified')

def build_batch_report_rows(urls, args, memory_summary):
    by_url={}
    for row in memory_summary: by_url[row['url']]=row  # last entry per URL wins
    rows=[]
    for i,url in enumerate(urls,1):
        info=classify_device(device_out_dir(url,args))
        mem=by_url.get(url)
        if info['category']=='Failed' and info['verified']==0:
            if mem is not None and mem['status']!='done':
                # No checkpoint was ever written for this URL (e.g. an invalid URL
                # skipped before any browser work started) — the in-memory reason from
                # the batch loop is more specific than the generic disk-based one.
                info['note']=mem['status']
            elif mem is None:
                info['note']='Not reached — the batch stopped before this device started'
        rows.append(dict(n=i,url=url,out=str(device_out_dir(url,args)),**info))
    return rows

def format_batch_report_text(rows):
    counts={'100% Successful':0,'Partially Successful':0,'Failed':0}
    lines=['Cashify batch report',utcnow(),'='*70]
    for r in rows:
        counts[r['category']]+=1
        lines.append(f"{r['n']:>3}. [{r['category']}] {r['url']}")
        progress=f"{r['verified']}/{r['planned']} cases verified" if r['planned'] else 'no cases planned yet'
        lines.append(f"     {progress}" + (f" — {r['note']}" if r['note'] else ''))
        lines.append(f"     {r['out']}")
    lines.append('-'*70)
    lines.append(f"Totals ({len(rows)} device(s)): {counts['100% Successful']} fully successful, "
                f"{counts['Partially Successful']} partially successful, {counts['Failed']} failed.")
    return '\n'.join(lines)

_LIVE_BATCH=dict(urls=None,args=None,summary=None)  # updated by run_batch; read by the close handler below
_console_handler_ref=None  # must stay alive for as long as the handler is registered

def install_windows_close_handler():
    """Best effort only: closing the console window (the X button), logging off, or a
    shutdown normally kill the process with no chance for any Python code — including
    our KeyboardInterrupt handling — to run at all. On Windows this registers a native
    console-control handler that gets a brief window to write whatever report data is
    currently available before the OS proceeds with terminating the process. No-op
    (and perfectly safe) everywhere else, or if anything about this fails to set up."""
    if sys.platform!='win32': return
    try:
        import ctypes,ctypes.wintypes as wintypes
        HANDLER_ROUTINE=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.DWORD)
        def handler(ctrl_type):
            try:
                if _LIVE_BATCH['urls'] is not None:
                    write_batch_report(_LIVE_BATCH['urls'],_LIVE_BATCH['args'],_LIVE_BATCH['summary'] or [])
            except Exception:
                pass
            return False  # we only observe; let Windows continue its normal shutdown of the process
        global _console_handler_ref
        _console_handler_ref=HANDLER_ROUTINE(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref,True)
    except Exception:
        pass

def write_batch_report(urls, args, memory_summary):
    """Rebuilds the report from scratch every time it's called, straight off each
    device's own checkpoint.json on disk. Called after every device finishes AND right
    before a KeyboardInterrupt is allowed to propagate, so the file on disk is never more
    than one device behind reality — including when the run is aborted partway through."""
    rows=build_batch_report_rows(urls,args,memory_summary)
    text=format_batch_report_text(rows)
    report_dir=Path('runs').resolve();report_dir.mkdir(parents=True,exist_ok=True)
    write_json(report_dir/'Batch_Report.json',rows)
    (report_dir/'Batch_Report.txt').write_text(text,encoding='utf-8')
    with (report_dir/'Batch_Report.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['n','category','verified','planned','url','out','note'])
        writer.writeheader()
        for r in rows: writer.writerow(r)
    return text,report_dir

async def process_device(context, url, args):
    """Run the full audit for one device URL in an already-open browser context.
    Raises on any stop condition for THIS device; the caller decides whether that
    ends the whole batch or just moves on to the next queued URL."""
    url=validate_url(url)
    out=device_out_dir(url,args);out.mkdir(parents=True,exist_ok=True)
    lock=out/'.running.lock'
    try: fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f'{lock} exists. Close the other run; after a crash remove this lock manually.')
    os.write(fd,str(os.getpid()).encode());os.close(fd)
    state=None;page=None;case=None;key=None
    try:
        checkpoint=out/'checkpoint.json'
        # explicit encoding='utf-8': these files are always WRITTEN as utf-8 (write_json/
        # export below), but Path.read_text() with no encoding falls back to the OS's
        # default codepage — cp1252 ("charmap") on most Windows machines — which cannot
        # decode the ₹ / curly-quote / em-dash bytes Cashify's own page text contains,
        # and raises UnicodeDecodeError (itself a ValueError) only once a device actually
        # has saved content to resume, not on a brand-new device's first run.
        state=json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else dict(
            version=VERSION,url=url,started_at=utcnow(),variants={})
        if state['url']!=url: raise ValueError('Output directory belongs to another URL. Use --output NEW_FOLDER.')
        state['runner_version']=VERSION
        state.pop('error',None)
        rules=json.loads(Path(args.rules).read_text(encoding='utf-8')) if args.rules else {}
        try:
            page=await context.new_page()
            found=await variants(page,url,args.timeout)
            state['variant_urls']=[item['url'] for item in found]
            export(out,state)
            for item in found:
                key=item['url']
                v=state['variants'].get(key)
                if v is None:
                    v=await calculator(page,key,args.timeout)
                    v.update(schema=dict(initial=[],groups=[],branches=[],functional=[],accessories=[],ages=[]),results={})
                    state['variants'][key]=v
                ui=UI(page,rules,args.timeout)
                schema=v['schema']
                if not v.get('plan'):
                    # Rebuild interrupted discovery from scratch; never reuse a partial schema.
                    schema=dict(initial=[],groups=[],branches=[],functional=[],accessories=[],ages=[])
                    v['schema']=schema
                    base=await ui.run_case(v,schema,dict(id='baseline',label='Flawless baseline'),True)
                    if base['quote']['status']!='quoted': raise ReviewRequired('Flawless route returned no offer.')
                    if v.get('discovery'):
                        v.setdefault('discovery_history',[]).append(v['discovery'])
                    v['discovery']=dict(baseline=base,branches={})
                    export(out,state)
                    for index,group in enumerate(schema['groups'][:],1):
                        print(f"Discovering {v['model']}: branch {index}/{len(schema['groups'])} — {group}",flush=True)
                        probe=await ui.run_case(v,schema,dict(id='discovery',label='Branch exploration',group=group),True)
                        v['discovery']['branches'][group]=probe
                        export(out,state)
                    v['plan']=plan(schema)
                    v['results']={'baseline':base}
                    export(out,state)
                print(f"\n{v['model']}: {len(v['plan'])} applicable cases",flush=True)
                for case in v['plan']:
                    if case['id'] in v['results']:
                        if case.get('control') and any(v['results'][case['id']]['quote'].get(k) !=
                                v['results']['baseline']['quote'].get(k) for k in
                                ['status','base','selling','imei_fee','processing_fee','total']):
                            raise ReviewRequired('Previously saved healthy control differs; use a fresh output directory.')
                        continue
                    # Each interrupted attempt restarts from the initial form.
                    for attempt in range(args.retries+1):
                        try:
                            row=await ui.run_case(v,schema,case)
                            break
                        except (SiteBlocked,ReviewRequired): raise
                        except Exception:
                            if attempt==args.retries: raise
                    v['results'][case['id']]=row
                    export(out,state)
                    q=row['quote'];base=v['results']['baseline']['quote']
                    if case.get('control') and any(q.get(k)!=base.get(k) for k in
                            ['status','base','selling','imei_fee','processing_fee','total']):
                        raise ReviewRequired('Healthy branch control changed the quote: '+case['label'])
                    deduction=base['selling']-q['selling'] if q['status']=='quoted' else 'unavailable'
                    print(f"{len(v['results'])}/{len(v['plan'])} {case['label']}: {q['selling']} INR; deduction {deduction}",flush=True)
                if not v.get('final_control'):
                    v['final_control']=await ui.run_case(v,schema,dict(id='final-control',label='Final flawless control'))
                export(out,state)
                if not v['final_control_matches']:
                    raise ReviewRequired('Final flawless quote changed. Results preserved; start a fresh output run for consistent baselines.')
            export(out,state)
            print(f"\n{state['status'].upper()}: {state['verified_cases']}/{state['planned_cases']} cases.\n{out/'Cashify_Test_Results.zip'}")
        finally:
            if sys.exc_info()[0] is not None and page is not None:
                try:
                    snapshot = await UI(page,rules,args.timeout).snapshot()
                    write_json(out/'diagnostic.json', dict(error=str(sys.exc_info()[1]),
                               url=page.url, observed_at=utcnow(), snapshot=snapshot,
                               case=case, variant=key))
                except Exception:
                    pass  # Preserve the original failure even if the page is already closed.
            if page is not None:
                await page.close()  # Close only this device's page; the shared context stays open for the next URL.
    except BaseException as e:
        if state is not None:
            state['error']=f'{type(e).__name__}: {e}'
            export(out,state)
        raise
    finally:
        lock.unlink(missing_ok=True)
    return out

async def run_batch(urls, args):
    """Run every URL back-to-back in one shared, already-logged-in browser context,
    so a sign-in/OTP done for the first device carries over to the rest. A stop on
    one device (site block, unknown question, changed control, etc.) is recorded and
    the batch moves on to the next queued URL rather than requiring a manual restart.
    If the browser itself crashes or gets closed partway through (e.g. 'Failed to open
    a new tab'), it is relaunched once and the device that was running is retried
    automatically before giving up on it. Only Ctrl+C aborts the remaining queue."""
    from playwright.async_api import async_playwright
    # Each URL is validated lazily, per device below — not all up front — so one
    # malformed URL anywhere in the list only fails that device, not the whole batch.
    summary=[]
    _LIVE_BATCH.update(urls=urls,args=args,summary=summary)  # for install_windows_close_handler
    async with async_playwright() as pw:
        headless_mode = os.environ.get("HEADLESS", "false").lower() in ("true", "1", "yes")
        async def launch():
            context=await pw.chromium.launch_persistent_context(str(Path(args.profile).resolve()),
                        headless=headless_mode,viewport=dict(width=1440,height=1000))
            context.set_default_timeout(args.timeout*1000)
            return context
        context=await launch()
        try:
            for i,url in enumerate(urls,1):
                print(f"\n{'='*70}\nDevice {i}/{len(urls)}: {url}\n{'='*70}",flush=True)
                # Check the URL's own format up front, before touching the browser at
                # all, so a malformed/wrong-domain URL is skipped immediately without
                # wasting a page-open attempt or a browser relaunch on it. This is kept
                # separate from the try/except below on purpose: process_device can
                # itself raise other, unrelated ValueError subclasses during real work
                # (e.g. UnicodeDecodeError while reading a saved file) that must NOT be
                # mistaken for a bad URL and must still go through the normal retry path.
                try:
                    validate_url(url)
                except ValueError as e:
                    print(f"\nSKIPPED this device: {e}\nFix the URL and re-run it on its own "
                          "if needed; continuing with the next queued URL.",file=sys.stderr)
                    summary.append(dict(url=url,status=f'ValueError: {e}',out=None))
                    write_batch_report(urls,args,summary)
                    continue
                for attempt in (1,2):
                    try:
                        out=await process_device(context,url,args)
                        summary.append(dict(url=url,status='done',out=str(out)))
                        break
                    except (SiteBlocked,ReviewRequired) as e:
                        print(f"\nSTOPPED this device: {type(e).__name__}: {e}\n"
                              "Checkpoint/diagnostic saved for it; continuing with the next queued URL.",file=sys.stderr)
                        summary.append(dict(url=url,status=f'{type(e).__name__}: {e}',out=None))
                        break
                    except KeyboardInterrupt:
                        summary.append(dict(url=url,status='interrupted',out=None))
                        # The device's own checkpoint.json (written by process_device's
                        # own crash handling, which runs even on KeyboardInterrupt) may
                        # already hold partial progress for THIS device — the report
                        # below picks that up from disk, not from this in-memory status.
                        text,report_dir=write_batch_report(urls,args,summary)
                        print(f"\nInterrupted. Final report so far:\n{text}\n\n"
                              f"Saved to {report_dir/'Batch_Report.txt'} / .csv / .json",file=sys.stderr)
                        raise
                    except BaseException as e:
                        if attempt==1:
                            # Likely the browser crashed, got closed, or ran out of
                            # resources mid-batch rather than anything about this
                            # device's quote flow — relaunch fresh and retry once.
                            print(f"\nBrowser/page error on this device ({type(e).__name__}: {e}). "
                                  "Relaunching the browser and retrying this device once.",file=sys.stderr)
                            try: await context.close()
                            except Exception: pass
                            context=await launch()
                            continue
                        print(f"\nSTOPPED this device after a retry: {type(e).__name__}: {e}\n"
                              "Checkpoint/diagnostic saved for it; continuing with the next queued URL.",file=sys.stderr)
                        summary.append(dict(url=url,status=f'{type(e).__name__}: {e}',out=None))
                # Close any stray extra tab/popup left open by this device (e.g. an
                # unclosed login or image-preview window) so it can't accumulate and
                # destabilize the browser before the next device starts.
                for p in list(context.pages):
                    if not p.is_closed():
                        try: await p.close()
                        except Exception: pass
                # Rebuilt and saved after every single device — win or lose — so the
                # report on disk is never more than one device behind reality, however
                # the run eventually ends (including a hard process kill, where no more
                # Python code gets to run at all after this point).
                write_batch_report(urls,args,summary)
                # Proactively recycle the browser on a schedule rather than only ever
                # reacting to a crash after it's already happened — a long-lived browser
                # session accumulates memory/handle pressure over dozens of devices that
                # tends to show up as exactly this kind of recurring "failed to open a
                # new tab" crash in long batches.
                if args.recycle_every and i%args.recycle_every==0 and i<len(urls):
                    print(f"\nRecycling the browser after {i} device(s) (scheduled, not a crash).",flush=True)
                    try: await context.close()
                    except Exception: pass
                    context=await launch()
                # A real pause between devices, not just politeness: it's the difference
                # between a steady trickle of requests and a continuous burst, which is
                # what both browser stability and a site's own rate-limiting respond to.
                if args.delay and i<len(urls):
                    await asyncio.sleep(args.delay)
        finally:
            try: await context.close()
            except Exception: pass  # Already crashed/closed; nothing more to clean up.
    text,report_dir=write_batch_report(urls,args,summary)
    print(f"\n{'='*70}\n{text}\n{'='*70}\n"
          f"Saved to {report_dir/'Batch_Report.txt'} / .csv / .json")
    return summary

_INVISIBLE_CHARS=str.maketrans('', '', '\u200b\u200c\u200d\ufeff')  # zero-width space/joiners, BOM

def clean_pasted_line(raw):
    """Strip ordinary/non-breaking whitespace plus invisible characters some clipboards
    sneak in (zero-width spaces, a leading byte-order mark) that str.strip() alone leaves
    behind, so a URL that LOOKS clean to the eye is actually clean to the parser too."""
    return raw.translate(_INVISIBLE_CHARS).strip()

_URL_IN_TEXT=re.compile(r'https?://\S+',re.I)

def extract_url(line):
    """Pull the URL out of a noisy line instead of assuming the whole line IS the URL —
    e.g. a numbered item copied straight from this script's OWN confirmation screen
    ('12. https://www.cashify.in/...') rather than a clean URL list. Returns the line
    unchanged if no http(s) URL is found anywhere in it, so a plain URL (the normal
    case) and a non-URL string (like a file path, checked separately) both pass through
    untouched — only a line that has a URL embedded in surrounding text is changed."""
    m=_URL_IN_TEXT.search(line)
    return m.group(0) if m else line

def load_urls_from_file(path):
    """Read one URL per line from a text file; blank lines and lines starting with # are
    ignored, and a URL is extracted even if the line has other text around it. Shared by
    --urls-file and by dropping/pasting a file path at the interactive prompt below, so
    both behave identically."""
    urls=[];skipped=0
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line=clean_pasted_line(line)
        if not line or line.startswith('#'): continue
        url=extract_url(line)
        if url is line and not line.lower().startswith(('http://','https://')):
            # No http(s) URL anywhere in this line at all — most likely a stray header
            # or prompt line that ended up in the file by accident (for example, the
            # file turning out to be a copy of this script's own terminal output rather
            # than a clean URL list), not a URL with a typo. Drop it rather than queue
            # something that could only ever fail.
            skipped+=1;continue
        urls.append(url)
    if skipped:
        print(f'  ({skipped} line(s) in {Path(path).name} had no URL in them and were ignored.)')
    return urls

def _as_local_file(raw):
    """If raw looks like a path to an existing file rather than a URL, return that path —
    handling the surrounding double quotes Windows adds when you drag a file into a
    terminal window. Returns None for anything that isn't an existing local file."""
    if raw.lower().startswith(('http://','https://')): return None
    candidate=raw[1:-1] if len(raw)>=2 and raw[0]=='"' and raw[-1]=='"' else raw
    return candidate if candidate and Path(candidate).is_file() else None

def collect_urls_interactively():
    print('Paste one Cashify device URL per line, in order (or drop/paste a .txt file full of them). '
          'Press Enter on an empty line to start the batch.')
    urls,i,leading_blanks=[],1,0
    while True:
        try:
            raw=clean_pasted_line(input(f'Paste the ({i}) Cashify device URL: '))
        except EOFError:
            break  # piped/closed input: stop with whatever was queued, rather than looping forever
        if not raw:
            if urls: break  # a blank line after at least one URL means "that's all of them"
            # A single blank line before any URL is almost always a paste artifact — e.g.
            # a URL copied with a leading blank line above it, which the terminal delivers
            # as its own separate Enter press — not an intentional "finish with zero URLs".
            # Tolerate exactly one such blank and re-prompt; a second blank with nothing
            # queued yet is treated as a deliberate cancel, so this can't loop forever.
            leading_blanks+=1
            if leading_blanks>=2: break
            continue
        leading_blanks=0
        file_path=_as_local_file(raw)
        if file_path is not None:
            try:
                loaded=load_urls_from_file(file_path)
            except Exception as e:
                print(f'  Could not read {file_path}: {e}')
                continue
            if not loaded:
                print(f'  {file_path} had no URLs in it (blank lines/# comments only) — nothing added.')
                continue
            added=0
            for u in loaded:
                if u in urls:
                    print(f'  Note: already queued — skipping duplicate from {Path(file_path).name}: {u}')
                else:
                    urls.append(u);added+=1
            print(f'  Loaded {added} URL(s) from {Path(file_path).name} ({len(urls)} queued so far).')
            i=len(urls)+1
            continue
        raw=extract_url(raw)  # pull a URL out of a noisy pasted line, e.g. "12. https://..."
        if not raw.lower().startswith(('http://','https://')):
            print(f"  Skipping — no https://www.cashify.in URL found in that line. If you meant to drop "
                  f"a file of URLs, paste its path instead.")
            continue
        if raw in urls:
            # Common paste artifact: a spreadsheet column fill-handle dragged past the
            # last real row repeats the last value for every extra row.
            print(f'  Note: already queued as #{urls.index(raw)+1} — added again anyway. '
                  'If that was an accidental paste, press Ctrl+C now instead of continuing.')
        urls.append(raw);i+=1
    if urls:
        print(f'\n{len(urls)} URL(s) queued:')
        for n,u in enumerate(urls,1): print(f'  {n}. {u}')
        if input(f'Run all {len(urls)}? [Y/n] ').strip().lower().startswith('n'):
            print('Cancelled — nothing was run. Start the script again and paste only the URLs you want.')
            return []
    return urls

def main():
    install_windows_close_handler()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('urls',nargs='*',help='One or more Cashify device/model URLs to run back-to-back; '
                        'omitted = interactive numbered prompts, one per line, blank line to start')
    parser.add_argument('--urls-file',help='Text file with one Cashify device URL per line '
                        '(blank lines and lines starting with # are ignored); combined with any URLs given on the command line')
    parser.add_argument('--output',help='New folder for fresh quotes; existing folder resumes verified cases. '
                        'Only valid with a single URL — a multi-URL batch always uses runs/<device-slug> per device')
    parser.add_argument('--profile',default='.cashify-browser',help='Separate local login profile; never your normal '
                        'Chrome profile. Shared across every URL in the batch, so you sign in at most once')
    parser.add_argument('--rules',help='JSON overrides for changed selectors or unrecognized branch blocks')
    parser.add_argument('--timeout',type=int,default=60)
    parser.add_argument('--retries',type=int,default=1,help='Transient Playwright failures only; unknown questions '
                        'and site blocks stop that device (and move on to the next queued one)')
    parser.add_argument('--delay',type=float,default=5,help='Seconds to pause between devices in a batch (default: '
                        '5). A long, unbroken run of back-to-back automated requests is the most common cause of a '
                        'browser destabilizing or Cashify itself throttling the session; pacing devices out reduces '
                        'both. Use 0 to disable')
    parser.add_argument('--recycle-every',type=int,default=8,help='Close and relaunch the browser after every N '
                        'devices even if nothing crashed (default: 8), instead of only reacting to a crash after '
                        'it happens — a long-lived browser session accumulates memory/handle pressure over dozens '
                        'of devices that this prevents. Use 0 to disable')
    args=parser.parse_args()
    urls=[extract_url(clean_pasted_line(u)) for u in args.urls]
    if args.urls_file: urls+=load_urls_from_file(args.urls_file)
    if not urls: urls=collect_urls_interactively()
    if not urls: print('No URLs given.',file=sys.stderr);sys.exit(1)
    if len(urls)>1 and args.output:
        print('--output cannot be combined with more than one URL; each device automatically gets '
              'its own runs/<device-slug> folder.',file=sys.stderr);sys.exit(1)
    try:
        summary=asyncio.run(run_batch(urls,args))
    except KeyboardInterrupt:
        print('\nStopped. Verified observations remain in checkpoint.json for every device already processed.')
        sys.exit(130)
    except Exception as e:
        print(f'\nSTOPPED: {e}\nSee the partial output and checkpoint; no result was fabricated.',file=sys.stderr)
        sys.exit(1)
    sys.exit(1 if any(row['status']!='done' for row in summary) else 0)

if __name__=='__main__': main()
