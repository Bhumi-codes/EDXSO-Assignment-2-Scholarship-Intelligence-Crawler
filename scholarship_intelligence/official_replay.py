"""Historical official-notice replay. No fixture facts or production DB writes."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from dateutil.parser import parse as parse_date
from scholarship_intelligence.settings import PROJECT_ROOT

IST = timezone(timedelta(hours=5, minutes=30))
CASES = [
    dict(key='nmmss', name='National Means-cum-Merit Scholarship Scheme', provider='Ministry of Education',
         cycle='2026-27', route='NSP student submission', tokens=['NMMSS', '2026-27'],
         sources=['https://www.pib.gov.in/newsite/erelcontent.aspx?lang=2&reg=48&relid=294184',
                  'https://www.pib.gov.in/PressReleaseIframePage.aspx?PRID=2317657&lang=2&reg=48'],
         expected=['2026-09-30', '2026-10-31'], notice_order=['2026-09-01', '2026-10-01']),
    dict(key='csss_renewal', name='Central Sector Scheme of Scholarships (CSSS) - renewal',
         provider='Sacred Heart College, Thevara', cycle='2026-27',
         route='CSSS renewal - student submission per Sacred Heart College notices',
         tokens=['CSSS', '2026', 'RENEWAL'],
         sources=['https://www.shcollege.ac.in/wp-content/uploads/Renewal-Applications-Open-for-Central-Sector-Scheme-of-Scholarships-2026\u20132027-1.pdf',
                  'https://www.shcollege.ac.in/news/extended-deadline-nsp-central-sector-scholarship-renewal-applications-31-october-2026/'],
         expected=['2026-09-30', '2026-10-31'], notice_order=['2026-09-28', '2026-10-13'],
         original_notice_reference='SHC/OFFICE/689/2026-27',
         scope_note='Revision in official institutional renewal instructions. Earlier notice dated 28 September supersedes older appendices; not an independently established national policy revision.'),
    dict(key='chevening_expiry', name='Chevening Scholarship - India', provider='British High Commission New Delhi',
         cycle='2025 application round', route='Indian citizens - Chevening Scholarship application',
         tokens=['Chevening Scholarships', 'Indian citizens', '2025'],
         sources=['https://www.gov.uk/government/news/applications-open-for-chevening-scholarships-to-study-in-the-uk'],
         expected=['2025-10-07'], expected_opening='2025-08-05'),
    dict(key='daad_hsk_expiry', name='DAAD HSK University Summer Courses Scholarship', provider='DAAD',
         cycle='2025-2026', route='DAAD HSK student scholarship application - India announcement',
         tokens=['DAAD', 'HSK', '2025', '2026'],
         sources=['https://www.daad.in/en/2025/08/29/daad-hsk-scholarship-2025-2026-applications-now-open/'],
         expected=['2025-10-30'], expected_opening='2025-09-01'),
]
DATE_PATTERN = r'(?:\d{1,2}[./]\d{1,2}[./]\d{4}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})'


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _date(text):
    return parse_date(text, dayfirst=bool(re.match(r'\d{1,2}[./]', text))).date().isoformat()


def _poppler():
    executable = shutil.which('pdftoppm')
    if not executable:
        raise ValueError('Scanned official notices require Poppler pdftoppm on PATH. No dates will be fabricated.')
    return executable


def _ocr_pages(paths, directory):
    """Windows built-in OCR; rendered pages and word coordinates remain inspectable."""
    if os.name != 'nt':
        raise ValueError('This replay currently requires Windows OCR for the scanned official notices.')
    script = r'''
param([string]$InputFile, [string]$OutputFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataWriter,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null
function Await($operation, $type) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
    $task = $method.MakeGenericMethod($type).Invoke($null, @($operation))
    try { $task.Wait() } catch { throw $task.Exception.InnerException }
    $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language('en-US')))
if ($null -eq $engine) { throw 'Install the Windows English OCR language capability.' }
$pages = @()
foreach ($image in (Get-Content -LiteralPath $InputFile -Raw | ConvertFrom-Json)) {
    $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
    $writer = New-Object Windows.Storage.Streams.DataWriter($stream)
    $writer.WriteBytes([System.IO.File]::ReadAllBytes($image))
    Await ($writer.StoreAsync()) ([uint32]) | Out-Null
    $stream.Seek(0)
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($line in $result.Lines) {
        $words = @($line.Words | ForEach-Object { @{text=$_.Text; x=$_.BoundingRect.X; y=$_.BoundingRect.Y; width=$_.BoundingRect.Width; height=$_.BoundingRect.Height} })
        $lines += @{text=$line.Text; words=$words}
    }
    $pages += @{lines=$lines; image=$image}
    $bitmap.Dispose(); $writer.DetachStream() | Out-Null; $writer.Dispose(); $stream.Dispose()
}
[System.IO.File]::WriteAllText($OutputFile, (ConvertTo-Json -InputObject $pages -Depth 10), (New-Object System.Text.UTF8Encoding($false)))
'''
    with TemporaryDirectory(prefix='ocr-', dir=directory) as temporary:
        base = Path(temporary)
        (base/'ocr.ps1').write_text(script, encoding='utf-8')
        _write(base/'input.json', [str(path.resolve()) for path in paths])
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(base/'ocr.ps1'),
                                 '-InputFile', str(base/'input.json'), '-OutputFile', str(base/'output.json')], capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise ValueError('Windows OCR failed: ' + result.stderr[-1500:])
        pages = json.loads((base/'output.json').read_bytes())
    _write(directory/'ocr.json', pages)
    return pages


def _fetch_prepare(url, root, label):
    from scholarship_intelligence.snapshots import save_raw_snapshot
    from scholarship_intelligence.preparation import prepare_snapshot
    session = requests.Session()
    session.headers['User-Agent'] = 'ScholarshipIntelligenceOfficialReplay/1.0'
    response = session.get(url, timeout=(10, 25))
    response.raise_for_status()
    if urlsplit(response.url).hostname != urlsplit(url).hostname:
        raise ValueError('Official notice redirected to a different host.')
    now = datetime.now(timezone.utc)
    raw = save_raw_snapshot(url, response.content, now, response.headers.get('Content-Type'), project_root=root)
    receipt = {'source_url':url, 'response_url':response.url, 'http_status':response.status_code,
               'retrieved_at':now.isoformat(), 'content_hash':raw.content_hash, 'raw_file_path':raw.raw_file_path}
    _write(root/'receipts'/(label+'.json'), receipt)
    pdf = response.content[:1024].lstrip().startswith(b'%PDF-')
    if not pdf:
        prepared = prepare_snapshot(raw, response.status_code, project_root=root)
        return json.loads((root/prepared.prepared_file_path).read_bytes()), receipt
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(response.content))
    texts = [page.extract_text(extraction_mode='layout') or '' for page in reader.pages]
    if all(text.strip() for text in texts):
        prepared = prepare_snapshot(raw, response.status_code, project_root=root)
        return json.loads((root/prepared.prepared_file_path).read_bytes()), receipt
    # OCR is explicit and confined to the replay; the normal PDF parser is unchanged.
    rendered = root/'rendered'/label
    rendered.mkdir(parents=True)
    # Poppler on Windows may reject long content-addressed paths.
    with TemporaryDirectory(prefix='pdf-', dir=root) as temporary:
        short = Path(temporary)
        (short/'notice.pdf').write_bytes(response.content)
        process = subprocess.run([_poppler(), '-scale-to', '2400', '-png', str(short/'notice.pdf'), str(short/'page')], capture_output=True, timeout=90)
        if process.returncode:
            raise ValueError('PDF rendering failed: '+process.stderr.decode(errors='replace')[-1000:])
        for image in short.glob('page-*.png'):
            shutil.copyfile(image, rendered/image.name)
    images = sorted(rendered.glob('page-*.png'), key=lambda path:int(path.stem.rsplit('-',1)[1]))
    if len(images) != len(reader.pages):
        raise ValueError('Incomplete rendering of physical PDF pages.')
    pages = _ocr_pages(images, rendered)
    snapshot_id = sha256((url+'\0'+raw.content_hash).encode()).hexdigest()
    version = 'pdf-text-ocr-windows-v1'
    prepared_path = Path('data/runtime/snapshots/prepared')/snapshot_id/(version+'.json')
    blocks = []
    for number,page in enumerate(pages,1):
        text = '\n'.join(line['text'] for line in page['lines'])
        if not text.strip():
            raise ValueError('OCR produced an empty physical page.')
        blocks.append(dict(block_id=f'p{number:06d}',kind='pdf_page',text=text,page_number=number,source_xpath=None,heading_path=[],links=[]))
    document = dict(snapshot_id=snapshot_id,source_url=url,content_hash=raw.content_hash,preparation_version=version,
                    raw_file_path=raw.raw_file_path,prepared_file_path=prepared_path.as_posix(),blocks=blocks,page_count=len(pages),
                    extraction_method='windows_ocr',ocr_artifact=(rendered/'ocr.json').relative_to(root).as_posix(),
                    rendered_pages=[dict(path=path.relative_to(root).as_posix(),sha256=sha256(path.read_bytes()).hexdigest()) for path in images])
    _write(root/prepared_path,document)
    return document, receipt


def _deadline(case, document, root):
    """Extract only scoped student deadlines. Expected test dates are not inputs."""
    text = '\n'.join(block['text'] for block in document['blocks'])
    for token in case['tokens']:
        if token.casefold() not in text.casefold():
            raise ValueError('Notice identity not established: '+token)
    choices = []
    if case['key']=='csss_renewal':
        return _csss_deadline(case, document)
    if case['key'] == 'chevening_expiry':
        return _expiry_window(case, document)[1:]
    if case['key'] == 'cbse' and 'Fresh Applications' in text and document.get('ocr_artifact'):
        pages = json.loads((root/document['ocr_artifact']).read_bytes())
        for page,block in zip(pages,document['blocks']):
            lines = page['lines']
            applicant = [line for line in lines if 'by Applicant' in line['text']]
            fresh = [line for line in lines if 'Fresh Applications' in line['text']]
            renewal = [line for line in lines if 'Renewal for' in line['text']]
            if len(applicant)!=1 or len(fresh)!=1 or len(renewal)!=1:
                continue
            words = applicant[0]['words']
            center = (min(w['x'] for w in words)+max(w['x']+w['width'] for w in words))/2
            lower = min(w['y'] for w in fresh[0]['words'])-30
            upper = min(w['y'] for w in renewal[0]['words'])-30
            for line in lines:
                for word in line['words']:
                    if re.fullmatch(DATE_PATTERN,word['text'],re.I) and lower < word['y'] < upper and abs(word['x']+word['width']/2-center)<100:
                        choices.append((_date(word['text']),block,dict(quote=word['text'],bounding_box=word,column='by Applicant',row='Fresh Applications')))
    else:
        patterns = {
            'nmmss': r'(?:extended to|(?:Last date for )?Submission of Applications?\s*[:\-]?)\s*('+DATE_PATTERN+r')',
            'ambassador': r'(?:last date of submission of applications for the Ambassador[’\']s scholarship is|has been extended to)\s*('+DATE_PATTERN+r')',
            'cbse': r'last date for submission of online applications\s+is\s+('+DATE_PATTERN+r')',
            'daad_hsk_expiry': r'Application Deadline:\s*('+DATE_PATTERN+r')',
            'mext_research': r'(?:deadline for submission is|Application Deadline\s*\(must\):)\s*('+DATE_PATTERN+r')',
            'mext_undergraduate': r'Application deadline:\s*('+DATE_PATTERN+r')',
        }
        for block in document['blocks']:
            for match in re.finditer(patterns[case['key']],block['text'],re.I):
                choices.append((_date(match[1]),block,dict(quote=match[0])))
    values = {entry[0] for entry in choices}
    if len(values)!=1:
        raise ValueError('Student deadline missing or ambiguous: '+str(sorted(values)))
    value,block,proof = choices[0]
    # Scope and column context must be preserved with the quote.
    proof.update(source_url=document['source_url'],snapshot_id=document['snapshot_id'],block_id=block['block_id'],page_number=block.get('page_number'),source_xpath=block.get('source_xpath'),source_context=block['text'],extraction_method=document.get('extraction_method','html'))
    return value,block,proof




def _csss_deadline(case, document):
    choices=[]
    is_pdf=document.get('extraction_method')=='windows_ocr' or document['preparation_version'].startswith('pdf-')
    for block in document['blocks']:
        if is_pdf:
            if case['original_notice_reference'] not in block['text']:
                continue
            if not re.search(r'Date\s*:\s*28-09-2026',block['text']):
                raise ValueError('Original CSSS notice date changed or its dated scope is missing.')
            pattern=r'student application open till\s*('+DATE_PATTERN+r')'
        else:
            if 'renewal applications' not in block['text'].casefold() or 'CSSS' not in block['text']:
                continue
            pattern=r'has been extended to\s*('+DATE_PATTERN+r')'
        for match in re.finditer(pattern,block['text'],re.I):
            proof=dict(quote=match[0],source_url=document['source_url'],snapshot_id=document['snapshot_id'],block_id=block['block_id'],
                page_number=block.get('page_number'),source_xpath=block.get('source_xpath'),source_context=block['text'],
                extraction_method=document.get('extraction_method','html'),scope=case['scope_note'])
            choices.append((_date(match[1]),block,proof))
    if not choices or len({row[0] for row in choices})!=1:
        raise ValueError('Scoped CSSS renewal deadline missing or ambiguous.')
    return choices[0]


def _expiry_window(case, document):
    choices=[]
    for block in document['blocks']:
        if case['key']=='chevening_expiry':
            match=re.search(r'will open from\s+(\d{1,2}\s+[A-Za-z]+)\s+to\s+(\d{1,2}\s+[A-Za-z]+\s+(\d{4}))',block['text'],re.I)
            if not match:
                continue
            opening=_date(match[1]+' '+match[3]); closing=_date(match[2])
            proof=dict(quote=match[0],source_url=document['source_url'],snapshot_id=document['snapshot_id'],
                       block_id=block['block_id'],page_number=block.get('page_number'),source_xpath=block.get('source_xpath'),
                       source_context=block['text'],extraction_method='html')
            choices.append((opening,closing,block,proof))
        else:
            match=re.search(r'Portal opens:\s*('+DATE_PATTERN+r')',block['text'],re.I)
            if match:
                choices.append((_date(match[1]),None,block,dict(quote=match[0],source_url=document['source_url'],
                    snapshot_id=document['snapshot_id'],block_id=block['block_id'],page_number=block.get('page_number'),
                    source_xpath=block.get('source_xpath'),source_context=block['text'],extraction_method='html')))
    if not choices or len({(row[0],row[1]) for row in choices})!=1:
        raise ValueError('Historical application opening date missing or ambiguous.')
    return choices[0]


def _window_status(candidate, as_of):
    opening=candidate.opening_date.value
    closing=candidate.closing_date.value
    if opening is None or closing is None or opening.stage!='student_application' or closing.stage!='student_application':
        return 'REVIEW_REQUIRED'
    if opening.route!=closing.route or opening.calendar_date>closing.calendar_date:
        return 'REVIEW_REQUIRED'
    if as_of>closing.calendar_date:
        return 'EXPIRED'
    return 'ACTIVE' if opening.calendar_date<=as_of<closing.calendar_date else 'REVIEW_REQUIRED'


def _candidate(case, document, deadline, block, root, *, opening=None, opening_block=None):
    from scholarship_intelligence.bundles import create_bundle
    from scholarship_intelligence.models import ScholarshipExtraction
    from scholarship_intelligence.evidence_validation import validate_evidence
    bundle = create_bundle([document['prepared_file_path']],case['name'],case['cycle'],project_root=root)
    payload = dict(schema_version='1.0',bundle_id=bundle.bundle_id,snapshot_ids=[document['snapshot_id']],evidence=[])
    for field in ScholarshipExtraction.model_fields:
        if field not in payload:
            payload[field] = dict(value=None,missing_reason='extraction_failed',explanation='Outside this deadline-only replay scope.',evidence_ids=[],inspected_snapshot_ids=[document['snapshot_id']])
    values = dict(scholarship_name=case['name'],provider=case['provider'],cycle=case['cycle'],official_source=document['source_url'],
                  closing_date=dict(calendar_date=deadline,stage='student_application',route=case['route']))
    if opening is not None:
        values['opening_date']=dict(calendar_date=opening,stage='student_application',route=case['route'])
    # Names are explicit normalized replay identities; source aliases and route
    # are checked above and retained with full document context, not inferred claims.
    for field,value in values.items():
        identifier='replay_'+field
        payload[field]=dict(value=value,missing_reason=None,explanation='Normalized official-notice replay identity.' if field in {'scholarship_name','provider','cycle'} else None,evidence_ids=[identifier],inspected_snapshot_ids=[document['snapshot_id']])
        cited = opening_block if field=='opening_date' else block
        if field in {'scholarship_name','provider','cycle'}:
            cited = max(document['blocks'], key=lambda b:sum(token.casefold() in b['text'].casefold() for token in case['tokens']))
        payload['evidence'].append(dict(evidence_id=identifier,snapshot_id=document['snapshot_id'],quote=cited['text'],location=dict(block_ids=[cited['block_id']],page_number=cited.get('page_number')),field_paths=[field]))
    candidate = ScholarshipExtraction.model_validate(payload)
    report = validate_evidence(candidate,root/bundle.bundle_file_path,project_root=root)
    if not report.evidence_checks_passed:
        raise ValueError('Exact source evidence checks failed: '+str(report.issues))
    return candidate,asdict(report)


def run_official_replay(*, project_root=PROJECT_ROOT):
    from scholarship_intelligence.comparison import compare_source_values
    root=Path(project_root).resolve()
    run=root/'data/runtime/official-notice-replay'/('run-'+uuid4().hex)
    run.mkdir(parents=True)
    database=run/'replay.sqlite3'
    checked=datetime.now(IST)
    changes,expired,failures=[],[],[]
    with sqlite3.connect(database) as connection:
        connection.executescript('CREATE TABLE records (case_key TEXT PRIMARY KEY, name TEXT, cycle TEXT, route TEXT, deadline TEXT, lifecycle_status TEXT); CREATE TABLE history (case_key TEXT, field TEXT, old_value TEXT, new_value TEXT, detected_at TEXT, evidence_json TEXT);')
        for case in CASES:
            print('Official replay: '+case['key'],flush=True)
            try:
                versions=[]
                for number,url in enumerate(case['sources']):
                    document,receipt=_fetch_prepare(url,run,case['key']+'-'+str(number+1))
                    value,block,proof=_deadline(case,document,run)
                    # Assertions follow extraction, never supply an extracted date.
                    if value != case['expected'][number]:
                        raise ValueError('Extracted date '+value+' differs from the reviewed expectation '+case['expected'][number]+'. Reinspect official notice.')
                    opening=opening_block=opening_proof=None
                    if case.get('expected_opening'):
                        opening,_,opening_block,opening_proof=_expiry_window(case,document)
                        if opening!=case['expected_opening']:
                            raise ValueError('Source opening date changed; recheck the historical window.')
                    candidate,evidence=_candidate(case,document,value,block,run,opening=opening,opening_block=opening_block)
                    if opening_proof:
                        proof['opening_evidence']=opening_proof
                    proof['evidence_validation']=evidence
                    candidate_path=run/'candidates'/(case['key']+'-'+str(number+1)+'.json')
                    _write(candidate_path,candidate.model_dump(mode='json'))
                    versions.append(dict(candidate=candidate,deadline=value,evidence=evidence,proof=proof,receipt=receipt,candidate_path=candidate_path.relative_to(root).as_posix()))
                with connection:
                    first=versions[0]
                    initial_status='HISTORICAL_BASELINE' if len(versions)==2 else _window_status(first['candidate'],first['candidate'].opening_date.value.calendar_date+timedelta(days=1))
                    connection.execute('INSERT INTO records VALUES (?,?,?,?,?,?)',(case['key'],case['name'],case['cycle'],case['route'],first['deadline'],initial_status))
                    if len(versions)==2:
                        later=versions[1]
                        if date.fromisoformat(later['deadline'])<=checked.date():
                            raise ValueError('The revised deadline has expired or is on its cutoff day; this case no longer qualifies for the unexpired change demonstration.')
                        differences=compare_source_values(first['candidate'],later['candidate'],official_notice_pair=tuple(case['sources']))
                        delta=next(item for item in differences if item['field']=='closing_date')
                        proof=dict(old=first['proof'],new=later['proof'],old_receipt=first['receipt'],new_receipt=later['receipt'],notice_order=case['notice_order'],notice_order_basis='Official preserved notice URLs and explicit extension wording; not crawl timestamps.',scope=case.get('scope_note','Official ministry notices for the same student application round.'))
                        if 'extend' not in later['proof']['source_context'].casefold():
                            raise ValueError('Later notice does not establish extension.')
                        connection.execute('INSERT INTO history VALUES (?,?,?,?,?,?)',(case['key'],'closing_date',first['deadline'],later['deadline'],checked.isoformat(),json.dumps(proof)))
                        connection.execute('UPDATE records SET deadline=?,lifecycle_status=? WHERE case_key=?',(later['deadline'],'NOT_EXPIRED',case['key']))
                        changes.append(dict(name=case['name'],cycle=case['cycle'],route=case['route'],field='closing_date',old_value=first['deadline'],new_value=later['deadline'],detected_at=checked.isoformat(),evidence=proof,candidates=[v['candidate_path'] for v in versions],evidence_checks_passed=True,change_status='changed',current_deadline_status='not expired',checked_at=checked.isoformat()))
                    else:
                        baseline_date=first['candidate'].opening_date.value.calendar_date+timedelta(days=1)
                        old_status=_window_status(first['candidate'],baseline_date)
                        status=_window_status(first['candidate'],checked.date())
                        if old_status!='ACTIVE' or status!='EXPIRED':
                            raise ValueError('Official window does not support the historical ACTIVE-to-EXPIRED transition.')
                        lifecycle_evidence=dict(source=first['proof'],historical_as_of=baseline_date.isoformat(),current_as_of=checked.isoformat(),
                            basis='Historical lifecycle evaluation of the same official window. No earlier live crawl is claimed; source dates remain unchanged.')
                        connection.execute('INSERT INTO history VALUES (?,?,?,?,?,?)',(case['key'],'lifecycle_status',old_status,status,checked.isoformat(),json.dumps(lifecycle_evidence)))
                        connection.execute('UPDATE records SET lifecycle_status=? WHERE case_key=?',(status,case['key']))
                        expired.append(dict(name=case['name'],cycle=case['cycle'],route=case['route'],opening_date=first['candidate'].opening_date.value.calendar_date.isoformat(),
                            closing_date=first['deadline'],old_value=old_status,new_value=status,lifecycle_status=status,historical_as_of=baseline_date.isoformat(),
                            checked_at=checked.isoformat(),source_values_changed=False,evaluation_method='historical_window_lifecycle_replay',
                            evidence=lifecycle_evidence,candidate=first['candidate_path'],evidence_checks_passed=True))
            except (OSError,ValueError,RuntimeError,requests.RequestException,subprocess.SubprocessError) as error:
                failures.append(dict(case=case['key'],error_type=type(error).__name__,error=str(error)))
    result=dict(operation='historical_official_notice_replay',simulated=False,live_change_observed=False,
                scope='Genuine preserved official-notice revisions replayed in order; Chevening and DAAD historical ACTIVE-to-EXPIRED window evaluation from freshly fetched official notices. Deadline-only demonstration, not whole-record verification.',
                changed_examples=len(changes),expired_examples=len(expired),target_met=len(changes)==2 and len(expired)==2,
                changes=changes,expired=expired,failures=failures,production_database_rows_changed=0,api_calls_made=0,
                checked_at=checked.isoformat(),replay_directory=run.relative_to(root).as_posix(),
                result_file=(run/'result.json').relative_to(root).as_posix(),database=database.relative_to(root).as_posix(),
                change_selection_policy='Only revisions whose later student deadline is after the actual check date qualify. CBSE 2025 is excluded from current selection; previous reports are preserved.')
    _write(run/'result.json',result)
    _write(root/'data/runtime/official-notice-replay/latest.json',result)
    return result
