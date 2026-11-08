"""Lock DJ version identity. Missed candidates are preferred to unsafe merges."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import dj_find_dupes_v2 as dj

from djlibreview.review import EXAMPLE_BYTES, EXAMPLE_SCOPE, analyze_csv_bytes

ROOT = Path(__file__).resolve().parents[1]


def key(name: str):
    parsed = dj.parse_dj_filename(name)
    return parsed['core'], parsed['signature']


class VersionIdentity(unittest.TestCase):
    def test_required_families_do_not_collapse(self) -> None:
        names = [
            'A - T (Clean).mp3',
            'A - T (Dirty).mp3',
            'A - T (Super Clean).mp3',
            'A - T (Intro).mp3',
            'A - T (Hype Intro).mp3',
            'A - T (Hype) (Intro).mp3',
            'A - T (Instrumental).mp3',
            'A - T (Acapella).mp3',
            'A - T (Acapella Intro).mp3',
            'A - T (Radio Edit).mp3',
            'A - T (Radio).mp3',
            'A - T (Extended).mp3',
            'A - T (Extended Mix).mp3',
            'A - T (VIP).mp3',
            'A - T (Dub).mp3',
            'A - T (DJ One Remix).mp3',
            'A - T (DJ Two Remix).mp3',
            'A - T (DJ Premier Edit).mp3',
            'A - T (Kaytranada Edit).mp3',
            'A - T (DJ Premier Remix).mp3',
            'A - T (Edit).mp3',
            'A - T (128-130 BPM Transition).mp3',
            'A - T (130-132 BPM Transition).mp3',
            'A - T (128 BPM Transition).mp3',
            'A - T (128BPM Transition).mp3',
            'A - T - 128-130 Transition.mp3',
            'A - T - 130-132 Transition.mp3',
            'A - T.mp3',
            'A - T (2).mp3',
            'A - T (Original).mp3',
            'A - T (Festival).mp3',
            'A - T Clean Intro.mp3',
        ]
        keys = [key(name) for name in names]
        self.assertEqual(len(keys), len(set(keys)))
        unlabeled = dj.parse_dj_filename('A - T.mp3')
        self.assertEqual(unlabeled['version_state'], 'unlabeled_uncertain')
        self.assertNotIn('original', unlabeled['signature'].lower())
        copy = dj.parse_dj_filename('A - T (2).mp3')
        self.assertTrue(copy['copy_hint'])
        self.assertIn('(2)', copy['core'])

    def test_named_editors_remixes_and_bpm_stay_apart(self) -> None:
        self.assertNotEqual(key('A - T (DJ Premier Edit).mp3'), key('A - T (Kaytranada Edit).mp3'))
        self.assertNotEqual(key('A - T (DJ Premier Edit).mp3'), key('A - T (DJ Premier Remix).mp3'))
        self.assertIn('dj premier edit', dj.parse_dj_filename('A - T (DJ Premier Edit).mp3')['signature'])
        self.assertNotEqual(
            key('A - T (128-130 BPM Transition).mp3'),
            key('A - T (130-132 BPM Transition).mp3'),
        )
        self.assertNotEqual(key('A - T (128 BPM Transition).mp3'), key('A - T (128BPM Transition).mp3'))
        self.assertNotEqual(key('A - T (Hype Intro).mp3'), key('A - T (Intro).mp3'))
        self.assertNotEqual(key('A - T (Hype Intro).mp3'), key('A - T (Hype) (Intro).mp3'))

    def test_true_filename_candidate_still_groups(self) -> None:
        scope = '/synthetic/dj-library'
        paths = [
            f'{scope}/a/A - T (Clean Intro).mp3',
            f'{scope}/b/A - T [Intro Clean].wav',
            f'{scope}/c/A - T (Dirty).mp3',
            f'{scope}/d/A - T (DJ Premier Edit).mp3',
            f'{scope}/e/A - T (Kaytranada Edit).mp3',
            f'{scope}/f/A - T (128-130 BPM Transition).mp3',
            f'{scope}/g/A - T (130-132 BPM Transition).mp3',
            f'{scope}/h/A - T.mp3',
            f'{scope}/i/A - T (2).mp3',
            f'{scope}/j/A - T (Original).mp3',
        ]
        groups = dj.candidate_groups(paths)
        self.assertEqual(len(groups), 1)
        grouped = [row['path'] for rows in groups.values() for row in rows]
        self.assertEqual(sorted(grouped), sorted(paths[:2]))

    def test_packaged_example_groups_only_real_filename_pairs(self) -> None:
        result = analyze_csv_bytes(
            EXAMPLE_BYTES,
            dataset_label='check',
            source_name='synthetic_inventory.csv',
            scope=EXAMPLE_SCOPE,
            snapshot_date='2026-03-01',
            path_column=None,
        )
        self.assertTrue(result.example_data)
        self.assertEqual(result.status, 'complete_filename_review')
        self.assertEqual(result.input_rows, 35)
        self.assertEqual(result.audio_paths, 33)
        self.assertEqual(result.repeated_path_rows, 1)
        self.assertEqual(result.unsupported_rows, 1)
        self.assertEqual(len(result.groups), 4)
        self.assertTrue(all(len(group.files) == 2 for group in result.groups.values()))
        grouped = [item.path for group in result.groups.values() for item in group.files]
        for banned in (
            'Dirty', 'Super Clean', 'Hype Intro', 'DJ Premier', 'Kaytranada', 'DJ One',
            'DJ Two', '128-130', '130-132', '96-128', '(2)', '(Original)', '(Festival)',
            'Radio Edit', '(Extended)', '(VIP)', '(Dub)', '(Instrumental)', '(Acapella)',
            '.txt',
        ):
            self.assertFalse(any(banned in path for path in grouped), banned)
        nova = next(group for group in result.groups.values() if any(path.endswith('(Clean Intro).mp3') for path in (item.path for item in group.files)))
        nova_names = {item.filename for item in nova.files}
        self.assertEqual(nova_names, {'Nova - Midnight (Clean Intro).mp3', 'Nova - Midnight [Intro Clean].flac'})
        self.assertIn('not a confirmed audio duplicate', nova.evidence_label)
        self.assertIn('Byte hashes were not checked', nova.uncertainty)
        self.assertIn('never authorizes deleting', nova.uncertainty)
        beyonce = next(group for group in result.groups.values() if any('world-composed' in item.path for item in group.files))
        self.assertTrue(any('world-decomposed' in item.path for item in beyonce.files))
        self.assertNotEqual(beyonce.files[0].path, beyonce.files[1].path)
        self.assertNotEqual(beyonce.files[0].filename, beyonce.files[1].filename)
        raw = (ROOT / 'djlibreview/examples/synthetic_inventory.csv').read_text(encoding='utf-8')
        self.assertIn('Beyonce\u0301 - E\u0301te\u0301 (Clean).mp3', raw)
        self.assertTrue(any(item.filename == 'Beyonce\u0301 - E\u0301te\u0301 (Clean).mp3' for item in beyonce.files))
        self.assertIn('Original filenames in this group are not identical', beyonce.comparison_note)


class EngineReportGuards(unittest.TestCase):
    def test_spreadsheet_cells_and_version_constants(self) -> None:
        self.assertEqual(dj.csv_cell('=HYPERLINK("http://evil.test")'), '\'=HYPERLINK("http://evil.test")')
        self.assertEqual(dj.csv_cell('+1'), "'+1")
        self.assertEqual(dj.csv_cell('@SUM(A1)'), "'@SUM(A1)")
        self.assertEqual(dj.csv_cell('/synthetic/dj-library/a.mp3'), '/synthetic/dj-library/a.mp3')
        self.assertEqual(dj.ENGINE_VERSION, '2.1.0')
        self.assertEqual(dj.SNAPSHOT_DATE_PROVENANCE, 'operator-supplied; not inferred from file mtime')

    def test_signature_json_stays_explicit(self) -> None:
        signature = json.loads(dj.parse_dj_filename('A - T (DJ Premier Edit).mp3')['signature'])
        self.assertEqual(signature['markers'], ['edit'])
        self.assertEqual(signature['qualifiers'], ['dj premier edit'])


if __name__ == '__main__':
    unittest.main()
