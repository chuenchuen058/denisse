"""Maths in cards renders in Anki and Obsidian, and the Anki card lets him type an answer."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tutor import anki, mathfmt  # noqa: E402
from tutor.conf import CONFIG  # noqa: E402

A = CONFIG["math"]["targets"]["anki"]
O = CONFIG["math"]["targets"]["obsidian"]


def wrapped(target, kind, body):
    a, b = target[kind]
    return f"{a}{body}{b}"


class Bare(unittest.TestCase):
    def test_bare_latex_after_words_is_wrapped_and_the_full_stop_stays_out(self):
        s = r"The solution is \mathbf{E} = \frac{1}{4\pi\varepsilon_0}\frac{q}{r^2}\,\hat{\mathbf{r}}."
        body = r"\mathbf{E} = \frac{1}{4\pi\varepsilon_0}\frac{q}{r^2}\,\hat{\mathbf{r}}"
        self.assertEqual(mathfmt.normalise(s, "anki"), "The solution is " + wrapped(A, "inline", body) + ".")

    def test_a_left_hand_side_joins_the_maths(self):
        s = r"E = \frac{kQ}{r^2} so about 5 kN/C"
        self.assertEqual(mathfmt.normalise(s, "anki"), wrapped(A, "inline", r"E = \frac{kQ}{r^2}") + " so about 5 kN/C")

    def test_a_whole_equation_line(self):
        s = r"\nabla^2\phi = \frac{\partial^2\phi}{\partial x^2} + \frac{\partial^2\phi}{\partial y^2}"
        self.assertEqual(mathfmt.normalise(s, "anki"), wrapped(A, "inline", s))

    def test_spaces_inside_braces_stay_in_one_piece(self):
        s = (r"\nabla\cdot\mathbf{F} = \frac{\partial F_x}{\partial x} + \frac{\partial F_y}{\partial y}"
             r" + \frac{\partial F_z}{\partial z}")
        self.assertEqual(mathfmt.normalise(s, "anki"), wrapped(A, "inline", s))
        s2 = r"Curl: \left(\frac{\partial F_z}{\partial y} - \frac{\partial F_y}{\partial z},\ \ldots\right) here"
        out = mathfmt.normalise(s2, "anki")
        self.assertEqual(out.count(A["inline"][0]), 1, out)

    def test_prose_is_left_alone(self):
        s = "The electric field is a vector field that exerts a force on charges."
        self.assertEqual(mathfmt.normalise(s, "anki"), s)
        self.assertEqual(mathfmt.normalise(s, "obsidian"), s)


class Delimited(unittest.TestCase):
    def assert_normalised(self, text, expected, target):
        result = mathfmt.normalise(text, target)
        self.assertEqual(result, expected)
        self.assertEqual(mathfmt.normalise(result, target), result)

    def test_each_style_becomes_the_targets_style(self):
        cases = [(r"Use \[ \vec{F} = I\vec{L}\times\vec{B} \] here", "display", r"\vec{F} = I\vec{L}\times\vec{B}"),
                 (r"Use $$ \oint B\cdot dl = \mu_0 I $$ here", "display", r"\oint B\cdot dl = \mu_0 I"),
                 (r"Use \(F = qE\) here", "inline", "F = qE"),
                 (r"Use $F = qE$ here", "inline", "F = qE")]
        for s, kind, body in cases:
            for name, target in (("anki", A), ("obsidian", O)):
                with self.subTest(s=s, target=name):
                    self.assertEqual(mathfmt.normalise(s, name), "Use " + wrapped(target, kind, body) + " here")

    def test_already_right_is_unchanged(self):
        s = "Gauss: " + wrapped(A, "inline", r"\oint \vec{E}\cdot d\vec{A} = Q/\epsilon_0")
        self.assertEqual(mathfmt.normalise(s, "anki"), s)

    def test_mixed_delimiters_and_bare_latex_are_normalised_once(self):
        text = r"Use \( x^2 \), $y^2$, \[ z^2 \], $$ w^2 $$ and \frac{1}{2}."
        for name, target in (("anki", A), ("obsidian", O)):
            with self.subTest(target=name):
                expected = ("Use " + wrapped(target, "inline", "x^2") + ", "
                            + wrapped(target, "inline", "y^2") + ", "
                            + wrapped(target, "display", "z^2") + ", "
                            + wrapped(target, "display", "w^2") + " and "
                            + wrapped(target, "inline", r"\frac{1}{2}") + ".")
                self.assert_normalised(text, expected, name)

    def test_currency_and_lone_dollars_are_prose(self):
        cases = ["It costs $5 and $10.", "It costs $5 and$10.",
                 "Prices: $5.50, $10.25, $1,000.", "A lone $ sign.",
                 "It costs $5", "The symbol is $", r"Escaped prices: \$5 and \$10."]
        for text in cases:
            for name in ("anki", "obsidian"):
                with self.subTest(text=text, target=name):
                    self.assert_normalised(text, text, name)

    def test_currency_beside_real_maths_preserves_both(self):
        for name, target in (("anki", A), ("obsidian", O)):
            cases = [
                (r"Price $5, maths $x^2$ then \frac{1}{2}.",
                 "Price $5, maths " + wrapped(target, "inline", "x^2")
                 + " then " + wrapped(target, "inline", r"\frac{1}{2}") + "."),
                (r"Use $x^2$ and \(y\) for $5 or $10.",
                 "Use " + wrapped(target, "inline", "x^2") + " and "
                 + wrapped(target, "inline", "y") + " for $5 or $10."),
                (r"Costs $5 and $10; use $$ x^2 $$ or \[ y^2 \].",
                 "Costs $5 and $10; use " + wrapped(target, "display", "x^2")
                 + " or " + wrapped(target, "display", "y^2") + "."),
                (r"Costs \$5; use $x^2$.",
                 r"Costs \$5; use " + wrapped(target, "inline", "x^2") + "."),
            ]
            for text, expected in cases:
                with self.subTest(text=text, target=name):
                    self.assert_normalised(text, expected, name)

    def test_numeric_maths_is_not_currency(self):
        for name, target in (("anki", A), ("obsidian", O)):
            with self.subTest(target=name):
                expected = ("Values " + wrapped(target, "inline", "2 + 3 = 5")
                            + " and " + wrapped(target, "inline", "5") + ".")
                self.assert_normalised("Values $2 + 3 = 5$ and $5$.", expected, name)

    def test_padding_inside_inline_dollars_is_trimmed(self):
        for body in ("x^2", r"\frac{1}{2}", "5", "5 + x", "5.50", "5,10"):
            for left, right in ((" ", " "), ("", " "), (" ", "")):
                for name, target in (("anki", A), ("obsidian", O)):
                    with self.subTest(body=body, left=left, right=right, target=name):
                        self.assert_normalised(f"Use ${left}{body}{right}$ here.",
                                               "Use " + wrapped(target, "inline", body) + " here.", name)

    def test_adjacent_maths_spans_have_unambiguous_target_delimiters(self):
        for text, kinds, bodies in (
                (r"\(x^2\)+\frac{1}{2}", ("inline", "inline"), ("x^2", r"+\frac{1}{2}")),
                (r"\(x^2\)\(y^2\)", ("inline", "inline"), ("x^2", "y^2")),
                (r"\[x^2\]\(y^2\)", ("display", "inline"), ("x^2", "y^2"))):
            for name, target in (("anki", A), ("obsidian", O)):
                with self.subTest(text=text, target=name):
                    expected = " ".join(wrapped(target, kind, body) for kind, body in zip(kinds, bodies))
                    self.assert_normalised(text, expected, name)

    def test_escaped_dollars_inside_maths_are_not_delimiters(self):
        body = r"x + \text{\$5}"
        for name, target in (("anki", A), ("obsidian", O)):
            for source in (A, O):
                for kind in ("inline", "display"):
                    with self.subTest(target=name, source=source, kind=kind):
                        self.assert_normalised(wrapped(source, kind, body),
                                               wrapped(target, kind, body), name)


class AnkiCard(unittest.TestCase):
    def test_fields_carry_anki_delimiters(self):
        card = {"front": "State Gauss's law.", "back": r"\oint \vec{E}\cdot d\vec{A} = \frac{Q}{\epsilon_0}",
                "source": "Atlas/x.md", "topic": "Gauss", "id": "don-1"}

        class P:
            vault = "C:/vault"
        f = anki.fields(P(), card)
        back = f[CONFIG["anki"]["field_of"]["back"]]
        self.assertTrue(back.startswith(A["inline"][0]) and back.endswith(A["inline"][1]), back)

    def test_front_has_an_optional_answer_box_and_back_shows_it(self):
        t = CONFIG["anki"]["templates"][0]
        self.assertIn("<textarea", t["Front"])
        self.assertIn("sessionStorage", t["Back"])
        self.assertNotIn("{{FrontSide}}", t["Back"], "the back must not bring back an empty answer box")

    def test_existing_note_type_gets_new_templates_and_css(self):
        calls = []

        def client(action, **p):
            calls.append((action, p))
            return [anki.note_type()] if action == "model_names" else None
        # ensure() needs a module only for the deck name; give it a stand-in
        orig = anki.deck_name
        anki.deck_name = lambda m: "D::M"
        try:
            anki.ensure(client, object())
        finally:
            anki.deck_name = orig
        names = [a for a, _ in calls]
        self.assertIn("update_templates", names)
        self.assertIn("update_styling", names)
        self.assertNotIn("create_model", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
