import unittest

from backend.pdf_parser.backend.backend_surya import _block_metrics, _document_body_size


class SuryaFontMetricsTest(unittest.TestCase):
    def test_body_text_uses_the_same_page_base_size(self):
        text_size = _block_metrics(
            "text", None, 500.0, 80.0, "x" * 80, body=14.0
        )[0]
        narrow_size = _block_metrics(
            "text", None, 100.0, 80.0, "x" * 80, body=14.0
        )[0]

        self.assertEqual(text_size, 14.0)
        self.assertEqual(narrow_size, 14.0)

    def test_document_body_size_is_shared_across_pages(self):
        blocks = [
            {"kind": "text", "w": 500.0, "h": 22.74, "text": "x" * 80},
            {"kind": "text", "w": 100.0, "h": 113.68, "text": "y" * 80},
        ]

        self.assertAlmostEqual(_document_body_size(blocks), 14.0, places=1)

    def test_captions_and_table_labels_are_smaller_than_body_text(self):
        body_size = _block_metrics(
            "text", None, 500.0, 80.0, "x" * 80, body=14.0
        )[0]
        caption_size = _block_metrics(
            "caption", None, 500.0, 80.0, "Caption", body=14.0
        )[0]
        table_size = _block_metrics(
            "table", None, 500.0, 80.0, "Table", body=14.0
        )[0]

        self.assertLess(caption_size, body_size)
        self.assertLess(table_size, body_size)


if __name__ == "__main__":
    unittest.main()
