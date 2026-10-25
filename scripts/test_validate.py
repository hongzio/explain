#!/usr/bin/env python3
"""Smoke tests for validate_doc.py.

    python3 scripts/test_validate.py

The validator is what keeps a generated document honest, so the rules worth
pinning here are the ones a plausible-looking document can quietly break: a
diagram node whose note is missing, a layer nothing reveals, a prediction that
says nothing back when the reader guesses wrong, and a diff doc's "before"
excerpt that never existed at the revision it claims.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VALIDATE_PY = str(Path(__file__).resolve().parent / "validate_doc.py")


def doc_html(body: str, lang: str = "en") -> str:
    return (
        f'<!DOCTYPE html><html lang="{lang}"><head><title>t</title></head><body>'
        f'<main id="explain-content">{body}</main></body></html>'
    )


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.doc = self.project / ".explain" / "demo"
        self.doc.mkdir(parents=True)
        (self.doc / "doc.json").write_text(
            json.dumps(
                {
                    "title": "Demo",
                    "kind": "module",
                    "target": "src/pricing.py",
                    "lang": "en",
                    "created_at": "2026-10-25T00:00:00Z",
                    "updated_at": "2026-10-25T00:00:00Z",
                    "commit": "abc1234",
                }
            ),
            encoding="utf-8",
        )
        self.src = self.project / "src"
        self.src.mkdir()
        (self.src / "pricing.py").write_text("def total(order):\n    return order.net\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_validator(self, body: str) -> tuple[int, str]:
        (self.doc / "index.html").write_text(doc_html(body), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, VALIDATE_PY, str(self.doc), "--project", str(self.project)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode, r.stdout + r.stderr

    def codes(self, out: str, kind: str = "ERROR") -> set:
        found = set()
        for line in out.splitlines():
            if not line.startswith(kind):
                continue
            token = line.split(None, 2)[1].rstrip(":")
            found.add(token)
        return found

    # ---- diagram node notes (E12 / W11) ----

    DIAGRAM = (
        '<section data-view="home" data-title="Home">'
        '<h2>Home</h2>'
        '<figure class="diagram" data-asks="Where does a price get rounded?">'
        '<svg viewBox="0 0 100 50">{nodes}</svg>'
        '<figcaption>Rounding happens in total(), not at the edge.'
        '<ol class="ex-legend">{legend}</ol></figcaption>'
        '</figure></section>'
    )

    def test_node_with_matching_legend_row_passes(self):
        code, out = self.run_validator(
            self.DIAGRAM.format(
                nodes='<g class="ex-node" data-node="total" data-ref="src/pricing.py:1">'
                '<rect fill="var(--ex-surface)"/><text>total</text></g>',
                legend='<li data-for="total">Adds the net up and rounds once.</li>',
            )
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.codes(out), set())

    def test_node_without_data_node_is_an_error(self):
        _code, out = self.run_validator(
            self.DIAGRAM.format(
                nodes='<g class="ex-node"><rect fill="none"/><text>total</text></g>',
                legend='<li data-for="total">Adds the net up.</li>',
            )
        )
        self.assertIn("E12", self.codes(out))

    def test_node_without_a_legend_row_is_an_error(self):
        _code, out = self.run_validator(
            self.DIAGRAM.format(
                nodes='<g class="ex-node" data-node="total"><rect fill="none"/><text>total</text></g>',
                legend='<li data-for="elsewhere">Unrelated.</li>',
            )
        )
        self.assertIn("E12", self.codes(out))
        self.assertIn("W11", self.codes(out, "WARN"))

    def test_diagram_without_asks_or_refs_warns(self):
        _code, out = self.run_validator(
            '<section data-view="home" data-title="Home"><h2>Home</h2>'
            '<figure class="diagram"><svg viewBox="0 0 10 10"><rect fill="none"/></svg>'
            '<figcaption>A picture.</figcaption></figure></section>'
        )
        warns = self.codes(out, "WARN")
        self.assertIn("W7", warns)
        self.assertIn("W8", warns)

    # ---- layers (E13) ----

    def layered(self, groups: str) -> str:
        return (
            '<section data-view="home" data-title="Home"><h2>Home</h2>'
            '<figure class="diagram" data-asks="How does a request reach the cache?" data-layers>'
            f'<svg viewBox="0 0 100 50">{groups}</svg>'
            '<figcaption>The cache is read inside the handler.</figcaption>'
            '</figure></section>'
        )

    def test_contiguous_labelled_layers_pass(self):
        code, out = self.run_validator(
            self.layered(
                '<g data-layer="0"><rect fill="none"/></g>'
                '<g data-layer="1" data-layer-label="Request"><rect fill="none"/></g>'
            )
        )
        self.assertEqual(code, 0, out)

    def test_layer_gap_is_an_error(self):
        _code, out = self.run_validator(
            self.layered(
                '<g data-layer="0"><rect fill="none"/></g>'
                '<g data-layer="2" data-layer-label="Cache"><rect fill="none"/></g>'
            )
        )
        self.assertIn("E13", self.codes(out))

    def test_unlabelled_layer_is_an_error(self):
        _code, out = self.run_validator(
            self.layered(
                '<g data-layer="0"><rect fill="none"/></g>'
                '<g data-layer="1"><rect fill="none"/></g>'
            )
        )
        self.assertIn("E13", self.codes(out))

    def test_layer_outside_a_layered_figure_is_an_error(self):
        _code, out = self.run_validator(
            '<section data-view="home" data-title="Home"><h2>Home</h2>'
            '<figure class="diagram" data-asks="What runs first?">'
            '<svg viewBox="0 0 10 10"><g data-layer="1" data-layer-label="Later">'
            '<rect fill="none"/></g></svg>'
            '<figcaption>Only the first step is drawn. See src/pricing.py:1.</figcaption>'
            '</figure></section>'
        )
        self.assertIn("E13", self.codes(out))

    # ---- predictions (E14 / E15 / W9) ----

    PREDICT = (
        '<div class="ex-predict">'
        '<p class="ex-ask">What does total() return for a net of 10.004?</p>'
        '<ul class="ex-choices">'
        '<li>10.004<span class="ex-why">Nothing rounds on the way out.</span></li>'
        '<li data-correct>10.0<span class="ex-why">total() rounds once, at the end.</span></li>'
        '</ul>'
        '<div class="ex-reveal"><p>It rounds in total().</p></div>'
        '</div>'
    )

    def view(self, body: str) -> str:
        return f'<section data-view="home" data-title="Home"><h2>Home</h2><p>A point first.</p>{body}</section>'

    def test_well_formed_prediction_passes(self):
        code, out = self.run_validator(self.view(self.PREDICT))
        self.assertEqual(code, 0, out)

    def test_choice_without_feedback_is_an_error(self):
        broken = self.PREDICT.replace('<span class="ex-why">Nothing rounds on the way out.</span>', '')
        _code, out = self.run_validator(self.view(broken))
        self.assertIn("E14", self.codes(out))

    def test_prediction_without_reveal_is_an_error(self):
        broken = self.PREDICT.replace('<div class="ex-reveal"><p>It rounds in total().</p></div>', '')
        _code, out = self.run_validator(self.view(broken))
        self.assertIn("E14", self.codes(out))

    def test_two_correct_choices_is_an_error(self):
        broken = self.PREDICT.replace("<li>10.004", '<li data-correct>10.004')
        _code, out = self.run_validator(self.view(broken))
        self.assertIn("E14", self.codes(out))

    def test_prediction_may_not_open_a_view(self):
        _code, out = self.run_validator(
            f'<section data-view="home" data-title="Home"><h2>Home</h2>{self.PREDICT}</section>'
        )
        self.assertIn("E15", self.codes(out))

    def test_more_than_three_predictions_warns(self):
        _code, out = self.run_validator(self.view(self.PREDICT * 4))
        self.assertIn("W9", self.codes(out, "WARN"))

    # ---- before/after evidence at a git revision (E16 / W12) ----

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.project), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def commit_a_change(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("add", "-A")
        self.git("commit", "-qm", "before")
        (self.src / "pricing.py").write_text(
            "def total(order):\n    return round(order.net, 2)\n", encoding="utf-8"
        )

    def contrast(self, before: str, after: str = "return round(order.net, 2)") -> str:
        return self.view(
            '<div class="ex-contrast">'
            f'<div class="ex-side" data-label="Before"><pre data-src="src/pricing.py" data-rev="HEAD">{before}</pre></div>'
            f'<div class="ex-side" data-label="After"><pre data-src="src/pricing.py">{after}</pre></div>'
            '<div class="ex-note">Rounding moved into total(), so callers stop seeing long decimals.</div>'
            "</div>"
        )

    def test_before_excerpt_verified_against_the_revision(self):
        self.commit_a_change()
        code, out = self.run_validator(self.contrast("return order.net"))
        self.assertEqual(code, 0, out)

    def test_before_excerpt_that_never_existed_is_an_error(self):
        self.commit_a_change()
        _code, out = self.run_validator(self.contrast("return order.net * 2"))
        self.assertIn("E16", self.codes(out))

    def test_data_rev_outside_a_git_repo_warns_instead_of_failing(self):
        # no commit_a_change(): no repo at all, so the "after" pane has to match
        # the working tree as it stands for the only complaint to be the warning
        code, out = self.run_validator(self.contrast("return order.net", after="return order.net"))
        self.assertIn("W12", self.codes(out, "WARN"))
        self.assertEqual(code, 0, out)

    def test_contrast_without_a_note_is_an_error(self):
        self.commit_a_change()
        body = self.contrast("return order.net").replace(
            '<div class="ex-note">Rounding moved into total(), so callers stop seeing long decimals.</div>', ""
        )
        _code, out = self.run_validator(body)
        self.assertIn("E17", self.codes(out))

    def test_contrast_pane_without_a_label_is_an_error(self):
        self.commit_a_change()
        body = self.contrast("return order.net").replace(' data-label="After"', "")
        _code, out = self.run_validator(body)
        self.assertIn("E17", self.codes(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
