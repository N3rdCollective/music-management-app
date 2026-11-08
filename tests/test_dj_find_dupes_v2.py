import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import dj_find_dupes_v2 as dj


def key(name):
    parsed = dj.parse_dj_filename(name)
    return parsed['core'], parsed['signature']


class FilenameSemantics(unittest.TestCase):
    def same(self, a, b):
        self.assertEqual(key(a), key(b))

    def different(self, a, b):
        self.assertNotEqual(key(a), key(b))

    def test_clean_intro_reorder(self):
        self.same('A - Tune (Clean Intro).mp3', 'A - Tune [Intro Clean].wav')

    def test_redrum_and_reintro_aliases(self):
        self.same('A - Tune (re-drum).mp3', 'A - Tune (redrum).mp3')
        self.same('A - Tune (re-intro).mp3', 'A - Tune (reintro).mp3')

    def test_separator_aliases(self):
        self.same('A - Tune (super_clean quick-hit).mp3', 'A - Tune (QuickHit SuperClean).mp3')
        self.same('A - Tune - Clean Intro.mp3', 'A - Tune (Intro Clean).mp3')

    def test_phrase_longest_first(self):
        tags = json.loads(dj.parse_dj_filename('A - T (Radio Edit Hype Intro Club Mix).mp3')['signature'])['markers']
        self.assertEqual(tags, ['club mix', 'hype intro', 'radio edit'])

    def test_remixers_stay_separate(self):
        self.different('A - T (DJ One Remix).mp3', 'A - T (DJ Two Remix).mp3')
        self.different('A - T (DJ Clean Remix).mp3', 'A - T (Clean DJ Remix).mp3')
        self.assertIn('dj one remix', dj.parse_dj_filename('A - T (DJ One Remix).mp3')['signature'])

    def test_extended_radio_quick(self):
        variants = [key('A - T (' + x + ').mp3') for x in ['extended', 'radio edit', 'quick hit', 'short edit']]
        self.assertEqual(len(set(variants)), 4)

    def test_clean_dirty_superclean(self):
        self.assertEqual(len({key('A - T (' + x + ').mp3') for x in ['clean', 'dirty', 'super clean']}), 3)

    def test_hypeintro_vs_intro(self):
        self.different('A - T (hype intro).mp3', 'A - T (intro).mp3')

    def test_vocal_acapella_inst(self):
        versions = ['vocal', 'acapella', 'inst', 'acapella intro', 'intro']
        self.assertEqual(len({key('A - T (' + x + ').mp3') for x in versions}), 5)
        self.same('A - T (acap).mp3', 'A - T (accapella).mp3')
        self.same('A - T (inst).mp3', 'A - T (instrumental).mp3')

    def test_dub_vip_dubplate(self):
        self.assertEqual(len({key('A - T (' + x + ').mp3') for x in ['dub', 'dubplate', 'vip']}), 3)

    def test_artists_and_title_words(self):
        for name in ['Dirty South - Clean', 'Clean Bandit - Rather Be', 'A vs B - Versus', 'A - Intro', 'A - Radio Edit']:
            parsed = dj.parse_dj_filename(name + '.mp3')
            self.assertEqual(parsed['core'], dj.normalized(name))
            self.assertEqual(parsed['version_state'], 'unlabeled_uncertain')
        self.different('A - T Versus.mp3', 'A - T.mp3')
        self.different('A - T (Versus).mp3', 'A - T.mp3')

    def test_unicode_preserved(self):
        self.same('Beyoncé - ÉTÉ (Clean).mp3', 'BEYONCE\u0301 - E\u0301te\u0301 (Clean).mp3')
        self.different('Beyoncé - Été.mp3', 'Beyonce - Ete.mp3')
        self.assertIn('זה לא', dj.parse_dj_filename('אמן - זה לא.mp3')['core'])

    def test_numbers_and_copy_suffix_conservative(self):
        self.different('A - Part 1.mp3', 'A - Part 2.mp3')
        self.different('A - T Vol 1.mp3', 'A - T Vol 2.mp3')
        self.different('A - T (Version 1).mp3', 'A - T (Version 2).mp3')
        self.different('A - T (2).mp3', 'A - T.mp3')
        self.assertTrue(dj.parse_dj_filename('A - T (2).mp3')['copy_hint'])
        self.assertIn('(2)', dj.parse_dj_filename('A - T (2).mp3')['core'])

    def test_style_families(self):
        names = ['phonk mix', 'jersey club remix', 'baltimore club', 'club mix', 'remix', 'mix']
        self.assertEqual(len({key('A - T (' + x + ').mp3') for x in names}), len(names))

    def test_unknown_qualifiers_retained(self):
        self.different('A - T (Festival Edit).mp3', 'A - T (Wedding Edit).mp3')
        self.different('A - T (Clean 2020).mp3', 'A - T (Clean 2021).mp3')
        self.different('A - T (Festival).mp3', 'A - T.mp3')
        self.different('A - T (Original).mp3', 'A - T.mp3')

    def test_unlabeled_not_original(self):
        parsed = dj.parse_dj_filename('A - T.mp3')
        self.assertEqual(parsed['version_state'], 'unlabeled_uncertain')
        self.assertNotIn('original', parsed['signature'].lower())

    def test_no_unstructured_suffix_guessing(self):
        self.different('A - T Clean Intro.mp3', 'A - T (Clean Intro).mp3')
        self.different('A - The Mix.mp3', 'A - The.mp3')

    def test_all_marker_aliases(self):
        for canonical, aliases in dj.ALIASES.items():
            for alias in aliases:
                with self.subTest(alias=alias):
                    # Versus on its own deliberately uncertain; mashup supplies context.
                    suffix = alias + (' mashup' if canonical == 'versus' else '')
                    tags = json.loads(dj.parse_dj_filename(f'A - T ({suffix}).mp3')['signature'])['markers']
                    self.assertIn(canonical, tags)

    def test_supported_formats(self):
        for extension in ['.mp3', '.m4a', '.wav', '.flac', '.aiff', '.aif']:
            self.assertIn(extension, dj.AUDIO_EXTENSIONS)


class IOAndProof(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'local'
        self.source.mkdir()

    def inventory(self, rows, headers=('path',)):
        path = self.base / 'index.csv'
        with path.open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def run_cli(self, inventory, *extra, output=None):
        output = output or self.base / 'report'
        with contextlib.redirect_stdout(io.StringIO()):
            rc = dj.main(['--inventory', str(inventory), '--scope', str(self.source), '--output', str(output), *extra])
        return rc, json.loads((output / 'summary.json').read_text())

    def test_inventory_no_audio_opens_and_row_dedup(self):
        # These source paths intentionally do not exist: report does not open/stat audio.
        rows = [[str(self.source / p / 'A - T (clean intro).mp3')] for p in ('one', 'two')]
        index = self.inventory(rows + [rows[0]])
        with patch.object(dj, 'safe_local_path', side_effect=AssertionError('must not inspect audio')):
            rc, summary = self.run_cli(index, '--snapshot-date', '2026-11-05')
        self.assertEqual(rc, 0)
        self.assertEqual(summary['audio_paths'], 2)
        self.assertEqual(summary['repeated_path_rows'], 1)
        self.assertEqual(summary['candidate_groups'], 1)
        self.assertEqual(summary['files_opened'], 0)
        report = (self.base / 'report' / 'filename_candidates.csv').read_text()
        self.assertIn('filename_candidates', report)
        self.assertNotIn('byte_identical_sha256', report)

    def test_out_of_scope_status_and_empty_paths(self):
        index = self.inventory([['/Volumes/share/A.mp3', 'ok'], ['', 'ok'],
                                [str(self.source / 'A.mp3'), 'error']], ('path', 'status'))
        rc, summary = self.run_cli(index)
        self.assertEqual(rc, 2)
        self.assertEqual(summary['error_count'], 3)
        self.assertEqual(summary['audio_paths'], 0)

    def test_ambiguous_headers_report_fatal(self):
        rc, summary = self.run_cli(self.inventory([], ('path', 'full_path')))
        self.assertEqual(rc, 2)
        self.assertEqual(summary['status'], 'fatal_error')

    def test_explicit_column_and_non_audio(self):
        index = self.inventory([[str(self.source / 'x.txt')]], ('custom',))
        rc, summary = self.run_cli(index, '--path-column', 'custom')
        self.assertEqual(rc, 0)
        self.assertEqual(summary['unsupported_rows'], 1)

    def test_missing_input_error_report(self):
        rc, summary = self.run_cli(self.base / 'absent.csv')
        self.assertEqual(rc, 2)
        self.assertEqual(summary['error_count'], 1)

    def test_directory_symlinks_and_budget(self):
        (self.source / 'A.mp3').write_bytes(b'dummy')
        (self.source / 'link.mp3').symlink_to(self.source / 'A.mp3')
        (self.source / 'linked-dir').symlink_to(self.base, target_is_directory=True)
        errors, counts = [], dj.defaultdict(int)
        paths = list(dj.enumerate_local(self.source, 100, errors, counts))
        self.assertEqual(paths, [str(self.source / 'A.mp3')])
        self.assertEqual(len(errors), 2)
        errors, counts = [], dj.defaultdict(int)
        list(dj.enumerate_local(self.source, 1, errors, counts))
        self.assertTrue(any('budget' in e['error'] for e in errors))

    def test_hash_optin_generated_fixtures_only(self):
        paths = []
        for sub in ['a', 'b', 'c']:
            folder = self.source / sub
            folder.mkdir()
            file = folder / 'A - T (Clean).mp3'
            file.write_bytes(b'fixture-identical' if sub != 'c' else b'different-content')
            paths.append(str(file))
        groups = dj.candidate_groups(paths)
        group_id = next(iter(groups))
        errors = []
        self.assertEqual(dj.hash_selected(groups, [], None, 20, 1000, errors)['files_opened'], 0)
        result = dj.hash_selected(groups, [group_id], self.source, 20, 1000, errors)
        self.assertEqual(result['files_opened'], 3)
        self.assertEqual(errors, [])
        rows = groups[group_id]
        self.assertEqual(sum(r.get('proof') == 'byte_identical_sha256' for r in rows), 2)
        self.assertNotEqual(rows[0]['sha256'], rows[2]['sha256'])

    def test_hash_budget_and_symlink_errors(self):
        first = self.source / 'A - T.mp3'
        first.write_bytes(b'dummy')
        sub = self.source / 'b'
        sub.mkdir()
        second = sub / first.name
        second.symlink_to(first)
        groups = dj.candidate_groups([str(first), str(second)])
        errors = []
        result = dj.hash_selected(groups, list(groups), self.source, 20, 4, errors)
        self.assertEqual(result['files_opened'], 0)
        self.assertEqual(len(errors), 2)
        self.assertFalse(any(r.get('proof') for rows in groups.values() for r in rows))

    def test_hash_requires_scope_and_group(self):
        with self.assertRaises(ValueError):
            dj.hash_selected({}, ['bad'], None, 20, 1000, [])
        with self.assertRaises(ValueError):
            dj.hash_selected({}, ['bad'], self.source, 20, 1000, [])

    def test_output_write_errors_surface(self):
        bad = self.base / 'not-directory'
        bad.write_text('fixture')
        with self.assertRaises(OSError):
            dj.write_reports(bad, {}, {}, [])

    def test_cli_no_report_overwrite(self):
        index = self.inventory([])
        self.run_cli(index)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli(index)

    def test_symlinked_report_path_cannot_write_into_source(self):
        index = self.inventory([])
        link = self.base / 'report-link'
        link.symlink_to(self.source, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli(index, output=link)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_directory_budget_counts_empty_subdirectories(self):
        for name in ('one', 'two', 'three'):
            (self.source / name).mkdir()
        errors, counts = [], dj.defaultdict(int)
        self.assertEqual(list(dj.enumerate_local(self.source, 2, errors, counts)), [])
        self.assertTrue(any('budget' in e['error'] for e in errors))

    def test_duplicate_headers_and_bad_encoding_reported(self):
        index = self.inventory([], ('path', 'path'))
        rc, summary = self.run_cli(index)
        self.assertEqual(rc, 2)
        self.assertEqual(summary['status'], 'fatal_error')
        index.write_bytes(b'path\n\xff')
        rc, summary = self.run_cli(index, output=self.base / 'report-bad-encoding')
        self.assertEqual(rc, 2)
        self.assertEqual(summary['error_count'], 1)

    def test_hash_recheck_never_retains_stale_proof(self):
        paths = []
        for sub in ('one', 'two'):
            folder = self.source / sub
            folder.mkdir()
            file = folder / 'A - T.mp3'
            file.write_bytes(b'fixture')
            paths.append(str(file))
        groups = dj.candidate_groups(paths)
        dj.hash_selected(groups, list(groups), self.source, 20, 1000, [])
        self.assertTrue(all(r.get('proof') for rows in groups.values() for r in rows))
        errors = []
        result = dj.hash_selected(groups, list(groups), self.source, 20, 1, errors)
        self.assertEqual(result['files_opened'], 0)
        self.assertFalse(any(r.get('proof') for rows in groups.values() for r in rows))
        self.assertEqual(len(errors), 2)

    def test_network_mount_rejected_before_path_access(self):
        real_read_text = Path.read_text
        def fake_mounts(path, *a, **kw):
            if str(path) == '/proc/self/mountinfo':
                return '1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / /network rw - nfs server rw\n'
            return real_read_text(path, *a, **kw)
        with patch.object(Path, 'read_text', fake_mounts):
            with self.assertRaisesRegex(ValueError, 'filesystem: nfs'):
                dj.safe_local_path('/network/music/missing.mp3', '/network/music')


if __name__ == '__main__':
    unittest.main()
