"""Scope and evidence regressions for the official-notice replay (no network)."""
from pathlib import Path
import unittest
from unittest.mock import patch
from scholarship_intelligence.official_replay import CASES, _deadline


class OfficialReplayTests(unittest.TestCase):
    def document(self, text):
        return dict(source_url='https://www.pib.gov.in/notice',snapshot_id='test',blocks=[dict(block_id='b1',text=text)])

    def test_nmmss_excludes_nodal_verification_dates(self):
        document=self.document('NMMSS 2026-27\nSubmission of Application October 31, 2026\nInstitute Nodal Officer Verification November 15, 2026\nDistrict Nodal Officer Verification November 30, 2026')
        self.assertEqual(_deadline(CASES[0],document,Path('.'))[0],'2026-10-31')

    def test_expected_date_is_assertion_not_extraction_input(self):
        document=self.document('NMMSS 2026-27\nSubmission of Application October 29, 2026')
        self.assertEqual(_deadline(CASES[0],document,Path('.'))[0],'2026-10-29')

    def test_conflicting_student_deadlines_are_rejected(self):
        document=self.document('NMMSS 2026-27\nSubmission of Application September 30, 2026\nSubmission of Application October 31, 2026')
        with self.assertRaises(ValueError):
            _deadline(CASES[0],document,Path('.'))

    def test_daad_does_not_use_event_or_other_programme_dates(self):
        document=self.document('DAAD HSK 2025-2026\nPortal opens: 1 September 2025\nApplication Deadline: 30 October 2025\nInfo session: 3 September 2025\nOther programme deadline 30 April 2026')
        self.assertEqual(_deadline(CASES[3],document,Path('.'))[0],'2025-10-30')

    def test_chevening_window_shared_year_comes_from_source(self):
        from scholarship_intelligence.official_replay import _expiry_window
        document=self.document('Chevening Scholarships Indian citizens 2025\nThe application window will open from 5 August to 7 October 2025.')
        opening,closing,_,_=_expiry_window(CASES[2],document)
        self.assertEqual((opening,closing),('2025-08-05','2025-10-07'))

    def test_lifecycle_replay_requires_same_student_route_and_valid_window(self):
        from datetime import date
        from types import SimpleNamespace as NS
        from scholarship_intelligence.official_replay import _window_status
        start=NS(calendar_date=date(2025,9,1),stage='student_application',route='HSK')
        end=NS(calendar_date=date(2025,10,30),stage='student_application',route='HSK')
        candidate=NS(opening_date=NS(value=start),closing_date=NS(value=end))
        self.assertEqual(_window_status(candidate,date(2025,9,2)),'ACTIVE')
        self.assertEqual(_window_status(candidate,date(2026,10,15)),'EXPIRED')
        self.assertEqual(_window_status(candidate,date(2025,8,1)),'REVIEW_REQUIRED')
        self.assertEqual(_window_status(candidate,date(2025,10,30)),'REVIEW_REQUIRED')
        end.route='other'; self.assertEqual(_window_status(candidate,date(2026,10,15)),'REVIEW_REQUIRED')
        end.route='HSK'; end.stage='institution_verification'
        self.assertEqual(_window_status(candidate,date(2026,10,15)),'REVIEW_REQUIRED')

    def test_cbse_table_uses_fresh_applicant_column_not_school_or_renewal(self):
        def line(text,x,y):
            return dict(text=text,words=[dict(text=text,x=x,y=y,width=120,height=20)])
        lines=[line('Fresh Applications',100,200),line('Renewal for 2025',100,300),line('by Applicant',500,140),
               line('02.12.2025',500,240),line('12.12.2025',800,240),line('09.12.2025',500,340)]
        document=self.document('Single Girl Child Scholarship Fresh Applications 2025')
        document['ocr_artifact']='ocr.json'
        with patch.object(Path,'read_bytes',return_value=__import__('json').dumps([dict(lines=lines)]).encode()):
            self.assertEqual(_deadline(dict(key='cbse',tokens=['Single Girl Child Scholarship','2025']),document,Path('.'))[0],'2025-12-02')

    def test_cross_notice_comparison_requires_ordered_pair_cycle_and_route(self):
        from types import SimpleNamespace as NS
        from scholarship_intelligence.comparison import compare_source_values
        def candidate(url, deadline, cycle="2025", route="fresh"):
            payload={"official_source":{"value":url,"evidence_ids":["url"]},
                     "closing_date":{"value":{"calendar_date":deadline,"stage":"student_application","route":route},"evidence_ids":["date"]}}
            return NS(scholarship_name=NS(value="TEST Scholarship"),provider=NS(value="TEST Provider"),
                      official_source=NS(value=url),cycle=NS(value=cycle),
                      closing_date=NS(value=NS(stage="student_application",route=route)),model_dump=lambda **kwargs:payload)
        first=candidate("https://www.cbse.gov.in/earlier.pdf","2025-10-23")
        later=candidate("https://www.cbse.gov.in/later.pdf","2025-12-02")
        pair=(first.official_source.value,later.official_source.value)
        with self.assertRaises(ValueError):
            compare_source_values(first,later)
        self.assertEqual([row["field"] for row in compare_source_values(first,later,official_notice_pair=pair)],["closing_date"])
        for second, sources in [(later,pair[::-1]),(candidate(pair[1],"2025-12-02",cycle="2026"),pair),
                                (candidate(pair[1],"2025-12-02",route="renewal"),pair)]:
            with self.assertRaises(ValueError):
                compare_source_values(first,second,official_notice_pair=sources)

    def test_ambassador_revision_ignores_publication_date(self):
        document=self.document("Ambassador's Scholarship 2026-2027 Bhutanese students\nThe deadline has been extended to 21 October 2026.\n13 October, 2026")
        self.assertEqual(_deadline(dict(key='ambassador',tokens=['Ambassador','2026-2027','Bhutanese']),document,Path('.'))[0],'2026-10-21')

    def test_csss_latest_dated_notice_is_separate_from_older_appendices(self):
        document=dict(source_url='https://www.shcollege.ac.in/original.pdf',snapshot_id='test',preparation_version='pdf-text-ocr-windows-v1',
            blocks=[dict(block_id='p1',page_number=1,text='CSSS RENEWAL 2026-27 SHC/OFFICE/689/2026-27 Date : 28-09-2026 student application open till 30th September 2026'),
                    dict(block_id='p2',page_number=2,text='Older CSSS RENEWAL notice 2026 student application open till 25th October 2026')])
        self.assertEqual(_deadline(CASES[1],document,Path('.'))[0],'2026-09-30')
        document['blocks'][0]['text']=document['blocks'][0]['text'].replace('28-09-2026','24-07-2026')
        with self.assertRaises(ValueError):
            _deadline(CASES[1],document,Path('.'))

    def test_notice_pair_normalizes_unicode_url_encoding(self):
        from types import SimpleNamespace as NS
        from scholarship_intelligence.comparison import compare_source_values
        from pydantic import HttpUrl
        raw="https://www.shcollege.ac.in/notice–2026.pdf"
        def candidate(url, day):
            return NS(scholarship_name=NS(value="TEST Scholarship"),provider=NS(value="TEST College"),
                cycle=NS(value="2026-27"),official_source=NS(value=HttpUrl(url)),
                closing_date=NS(value=NS(stage="student_application",route="renewal")),
                model_dump=lambda **kwargs:{"closing_date":{"value":day,"evidence_ids":["quote"]}})
        before=candidate(raw,"2026-09-30");after=candidate("https://www.shcollege.ac.in/later","2026-10-31")
        self.assertEqual(len(compare_source_values(before,after,official_notice_pair=(raw,str(after.official_source.value)))),1)

    def test_missing_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            _deadline(CASES[0],self.document('Other scholarship Submission of Application October 31, 2026'),Path('.'))


if __name__=='__main__':
    unittest.main()
