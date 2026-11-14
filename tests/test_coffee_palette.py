"""Guard first-party UI sources against reintroducing the retired blue palette."""

import colorsys
from pathlib import Path
import re
import unittest


class CoffeePaletteTests(unittest.TestCase):
    def test_ui_sources_do_not_define_blue_or_violet_colors(self):
        root = Path(__file__).resolve().parents[1] / 'frontend'
        paths = [*root.joinpath('static').rglob('*.css'),
                 *root.joinpath('static').rglob('*.js'),
                 *root.joinpath('static').rglob('*.svg'),
                 *root.joinpath('templates').rglob('*.html')]
        violations = []
        for path in paths:
            source = path.read_text(encoding='utf-8')
            colors = []
            for match in re.finditer(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b', source):
                colors.append((match[0], tuple(int(match[0][i:i+2], 16) for i in (1, 3, 5))))
            for match in re.finditer(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', source):
                colors.append((match[0], tuple(map(int, match.groups()))))
            for literal, rgb in colors:
                hue, saturation, _ = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
                if 175 <= hue * 360 <= 285 and saturation > .035:
                    violations.append(f'{path.relative_to(root)}: {literal}')
        self.assertEqual(violations, [])
