"""Synthetic 100k-path smoke check. Not a published benchmark."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import dj_find_dupes_v2 as dj

from djlibreview.review import MAX_UPLOAD_BYTES, analyze_csv_bytes

SCOPE = '/synthetic/dj-library'


class LargeInventory(unittest.TestCase):
    def test_one_hundred_thousand_synthetic_paths(self) -> None:
        variants = [
            'Nova - Midnight (Dirty).mp3',
            'Nova - Midnight (Super Clean).mp3',
            'Nova - Midnight (Hype Intro).mp3',
            'Nova - Midnight (Intro).mp3',
            'Nova - Midnight (Instrumental).mp3',
            'Nova - Midnight (Acapella).mp3',
            'Nova - Midnight (Radio Edit).mp3',
            'Nova - Midnight (Extended).mp3',
            'Nova - Midnight (VIP).mp3',
            'Nova - Midnight (Dub).mp3',
            'Nova - Midnight (DJ Premier Edit).mp3',
            'Nova - Midnight (Kaytranada Edit).mp3',
            'Nova - Midnight (DJ One Remix).mp3',
            'Nova - Midnight (DJ Two Remix).mp3',
            'Nova - Midnight (128-130 BPM Transition).mp3',
            'Nova - Midnight (130-132 BPM Transition).mp3',
            'Nova - Midnight.mp3',
            'Nova - Midnight (2).mp3',
            'Nova - Midnight (Original).mp3',
        ]
        lines = ['path,status', f'{SCOPE}/a/Nova - Midnight (Clean).mp3,ok', f'{SCOPE}/b/Nova - Midnight (Clean).mp3,ok']
        for name in variants:
            lines.append(f'{SCOPE}/v/{name},ok')
        filler = 100_000 - (len(lines) - 1)
        for index in range(filler):
            lines.append(f'{SCOPE}/u/Track {index}.mp3,ok')
        self.assertEqual(len(lines) - 1, 100_000)
        data = ('\n'.join(lines) + '\n').encode('utf-8')
        self.assertLess(len(data), MAX_UPLOAD_BYTES)
        started = time.perf_counter()
        with patch.object(dj, 'enumerate_local', side_effect=AssertionError('scan')), \
                patch.object(dj, 'hash_selected', side_effect=AssertionError('hash')), \
                patch.object(dj, 'safe_local_path', side_effect=AssertionError('live path')), \
                patch('os.walk', side_effect=AssertionError('walk')):
            result = analyze_csv_bytes(
                data,
                dataset_label='Synthetic 100k',
                source_name='synthetic-100k.csv',
                scope=SCOPE,
                snapshot_date='2026-03-01',
                path_column=None,
            )
        elapsed = time.perf_counter() - started
        print(f'\n100k synthetic analyze seconds: {elapsed:.3f}')
        self.assertLess(elapsed, 60)
        self.assertEqual(result.audio_paths, 100_000)
        self.assertEqual(len(result.groups), 1)
        grouped = [item.filename for group in result.groups.values() for item in group.files]
        self.assertEqual(grouped, ['Nova - Midnight (Clean).mp3', 'Nova - Midnight (Clean).mp3'])
        self.assertFalse(result.example_data)
        payload_groups = next(iter(result.groups.values()))
        self.assertIn('not a confirmed audio duplicate', payload_groups.evidence_label)
        self.assertIn('Byte hashes were not checked', payload_groups.uncertainty)


if __name__ == '__main__':
    unittest.main()
