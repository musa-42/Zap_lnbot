"""Item 12: i18n pack parity + placeholder checker (pack level) — test-only.

t() (zap_i18n.py) swallows format KeyError and T() (zap_p2p.py) swallows all
format errors, so a pack whose {placeholder} set drifts from the code's
kwargs silently renders raw braces (or crashes t() on ValueError). These
checks pin pack health:

- EN and FA packs have exactly the same key set (214 = 214 today).
- Every other pack is a subset of EN keys (t() falls back to EN per key).
- Per key, every pack declares the same {name} placeholder set as EN.
- All placeholder fields are valid identifiers (kwargs-only formatting).
- Every string formats cleanly with a neutral value (balanced braces,
  valid format specs) so .format() cannot raise at call time.

Covers zap_i18n.TRANSLATIONS and the zap_p2p TXT packs. No t() behavior
change in this item. Gate for items 21-24.
"""
import ast
import string
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


def placeholders(s):
    return {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}


def _exec_assign(src_text, target_name, filename):
    """Extract and exec one top-level `NAME = {...}` assignment without
    importing the module (offline)."""
    ns = {}
    for node in ast.parse(src_text).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(tg, ast.Name) and tg.id == target_name for tg in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), filename, 'exec'), ns)
    return ns[target_name]


def translations():
    # TRANSLATIONS' values reference the EN/FA/... module-level dicts, so exec
    # the whole (pure, side-effect-free) module instead of one assignment.
    ns = {}
    exec(compile(ast.parse((ROOT / 'zap_i18n.py').read_text()), 'zap_i18n.py', 'exec'), ns)
    return ns['TRANSLATIONS']


def p2p_txt():
    return _exec_assign((ROOT / 'zap_p2p.py').read_text(), 'TXT', 'zap_p2p.py')


class ZapI18nPackParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs = translations()
        cls.en = cls.packs['en']

    def test_en_fa_exact_key_parity(self):
        self.assertEqual(set(self.packs['fa']), set(self.en))

    def test_other_packs_are_subsets_of_en(self):
        for lang, pack in self.packs.items():
            if lang == 'en':
                continue
            self.assertEqual(set(pack) - set(self.en), set(), lang)

    def test_placeholder_sets_match_en_per_key(self):
        for lang, pack in self.packs.items():
            if lang == 'en':
                continue
            for key, val in pack.items():
                self.assertEqual(placeholders(val), placeholders(self.en[key]),
                                 f'{lang}:{key}')

    def test_values_are_strings_with_identifier_placeholders(self):
        for lang, pack in self.packs.items():
            for key, val in pack.items():
                self.assertIsInstance(val, str, f'{lang}:{key}')
                for field in placeholders(val):
                    self.assertTrue(field.isidentifier(), f'{lang}:{key}:{field}')

    def test_every_string_formats_cleanly(self):
        for lang, pack in self.packs.items():
            for key, val in pack.items():
                try:
                    val.format(**{p: 1 for p in placeholders(val)})
                except Exception as e:
                    self.fail(f'{lang}:{key} not formattable: {e!r}')


class P2PTxtPackParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs = p2p_txt()
        cls.en = cls.packs['en']

    def test_fa_en_exact_key_parity(self):
        self.assertEqual(set(self.packs['fa']), set(self.en))

    def test_placeholder_sets_match_en_per_key(self):
        for lang, pack in self.packs.items():
            if lang == 'en':
                continue
            for key, val in pack.items():
                self.assertEqual(placeholders(val), placeholders(self.en[key]),
                                 f'{lang}:{key}')

    def test_values_are_strings_with_identifier_placeholders(self):
        for lang, pack in self.packs.items():
            for key, val in pack.items():
                self.assertIsInstance(val, str, f'{lang}:{key}')
                for field in placeholders(val):
                    self.assertTrue(field.isidentifier(), f'{lang}:{key}:{field}')

    def test_every_string_formats_cleanly(self):
        for lang, pack in self.packs.items():
            for key, val in pack.items():
                try:
                    val.format(**{p: 1 for p in placeholders(val)})
                except Exception as e:
                    self.fail(f'{lang}:{key} not formattable: {e!r}')


if __name__ == '__main__':
    unittest.main()
