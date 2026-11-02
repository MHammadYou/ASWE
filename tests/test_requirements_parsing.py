import unittest

from check_requirements import decode_requirements_bytes, parse_requirements_text, pinned_version


class LineCountingTests(unittest.TestCase):
    def test_counts_every_physical_line_including_blanks_and_comments(self):
        self.assertEqual(parse_requirements_text("# note\n\nflask==2.0\n").line_count, 3)

    def test_counts_last_line_without_trailing_newline(self):
        self.assertEqual(parse_requirements_text("a==1\nb==2").line_count, 2)

    def test_empty_file_has_zero_lines(self):
        self.assertEqual(parse_requirements_text("").line_count, 0)

    def test_continuation_counts_physical_lines_but_one_requirement(self):
        parsed = parse_requirements_text("flask==2.0 \\\n  --hash=sha256:abc\n")
        self.assertEqual(parsed.line_count, 2)
        self.assertEqual(len(parsed.requirements), 1)


class RequirementExtractionTests(unittest.TestCase):
    def test_extracts_name_and_pin_ignoring_extras_markers_and_inline_comments(self):
        parsed = parse_requirements_text('Requests[socks]==2.31.0 ; python_version > "3.7"  # http client\n')
        self.assertEqual(parsed.requirements[0].name, "Requests")
        self.assertEqual(pinned_version(parsed.requirements[0]), "2.31.0")

    def test_ranges_and_wildcards_are_not_pins(self):
        parsed = parse_requirements_text("flask>=2\nflask==2.*\nflask\n")
        self.assertEqual([pinned_version(item) for item in parsed.requirements], [None, None, None])

    def test_options_urls_and_garbage_are_skipped_with_a_reason(self):
        parsed = parse_requirements_text("-r base.txt\nfoo @ https://example.com/foo.zip\n./local\n")
        self.assertEqual(parsed.requirements, [])
        self.assertEqual(
            [reason for _, reason in parsed.unchecked_lines],
            ["pip option", "direct URL", "not a PyPI requirement"],
        )


class EncodingTests(unittest.TestCase):
    def test_decodes_powershell_utf16_redirect_output(self):
        raw = "flask==2.0\r\n".encode("utf-16")
        parsed = parse_requirements_text(decode_requirements_bytes(raw))
        self.assertEqual(pinned_version(parsed.requirements[0]), "2.0")

    def test_strips_utf8_byte_order_mark(self):
        parsed = parse_requirements_text(decode_requirements_bytes(b"\xef\xbb\xbfflask==2.0\n"))
        self.assertEqual(parsed.requirements[0].name, "flask")


if __name__ == "__main__":
    unittest.main()
