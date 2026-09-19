"""Execute the actual dependency-free editor logic in macOS's system JavaScriptCore.

No Node, packages or external network. Other platforms explicitly skip this engine harness.
"""

import ctypes
from pathlib import Path
import sys
import unittest

STATIC = Path(__file__).resolve().parents[1] / "src/job_search_assistant/adapters/web/static/js"


@unittest.skipUnless(sys.platform == "darwin", "System JavaScriptCore harness requires macOS")
class EditorLogicTests(unittest.TestCase):
    def setUp(self):
        self.js = ctypes.CDLL(
            "/System/Library/Frameworks/JavaScriptCore.framework/JavaScriptCore",
        )
        pointer = ctypes.c_void_p
        declarations = {
            "JSGlobalContextCreate": ([pointer], pointer),
            "JSGlobalContextRelease": ([pointer], None),
            "JSStringCreateWithUTF8CString": ([ctypes.c_char_p], pointer),
            "JSStringRelease": ([pointer], None),
            "JSEvaluateScript": ([pointer, pointer, pointer, pointer, ctypes.c_int,
                                  ctypes.POINTER(pointer)], pointer),
            "JSValueToStringCopy": ([pointer, pointer, ctypes.POINTER(pointer)], pointer),
            "JSStringGetMaximumUTF8CStringSize": ([pointer], ctypes.c_size_t),
            "JSStringGetUTF8CString": ([pointer, ctypes.c_char_p, ctypes.c_size_t],
                                      ctypes.c_size_t),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.js, name)
            function.argtypes, function.restype = arguments, result
        self.context = self.js.JSGlobalContextCreate(None)
        self.addCleanup(self.js.JSGlobalContextRelease, self.context)
        self.evaluate("const structuredClone = value => JSON.parse(JSON.stringify(value));")
        self.evaluate((STATIC / "editor-state.js").read_text().replace("export ", ""))
        self.evaluate('''
            const calls = [], notices = [];
            let serial = 0;
            const writer = new SaveQueue((action, body) => new Promise((resolve, reject) => {
              calls.push({action, body: structuredClone(body), resolve, reject});
            }), queue => notices.push({status: queue.status, items: queue.items()}),
              () => String(++serial));
            const draft = (version, role) => ({id: "draft", version, items: [
              {id: "stable-item", fields: {role}, status: "draft"},
            ]});
            writer.load(draft(1, "original"));
        ''')

    def evaluate(self, source):
        script = self.js.JSStringCreateWithUTF8CString(source.encode())
        error = ctypes.c_void_p()
        value = self.js.JSEvaluateScript(self.context, script, None, None, 1,
                                         ctypes.byref(error))
        self.js.JSStringRelease(script)
        string = self.js.JSValueToStringCopy(self.context, error.value or value, None)
        size = self.js.JSStringGetMaximumUTF8CStringSize(string)
        buffer = ctypes.create_string_buffer(size)
        self.js.JSStringGetUTF8CString(string, buffer, size)
        self.js.JSStringRelease(string)
        text = buffer.value.decode()
        self.assertFalse(error.value, text)
        return text

    def test_pending_saving_ack_order_and_new_input_survives_old_ack(self):
        self.evaluate('writer.enqueue("edit", {item_id: "stable-item", changes: {role: "A"}})')
        self.assertEqual("pending", self.evaluate("writer.status"))
        self.evaluate('writer.flush();')
        self.assertEqual("saving", self.evaluate("writer.status"))
        self.evaluate('writer.enqueue("edit", {item_id: "stable-item", changes: {role: "B"}})')
        self.assertEqual("1", self.evaluate("calls.length"))
        self.evaluate('calls[0].resolve(draft(2, "A"));')
        self.assertEqual("B", self.evaluate('writer.items()[0].fields.role'))
        self.assertEqual("2", self.evaluate("calls[1].body.expected_version"))
        self.assertEqual("stable-item", self.evaluate("calls[1].body.item_id"))
        self.assertNotEqual("saved", self.evaluate("writer.status"))
        self.evaluate('calls[1].resolve(draft(3, "B"));')
        self.assertEqual("saved", self.evaluate("writer.status"))
        self.assertEqual("B", self.evaluate('writer.draft.items[0].fields.role'))

    def test_failed_save_keeps_input_exact_retry_then_continues_new_edits(self):
        self.evaluate('''
            writer.enqueue("edit", {item_id: "stable-item", changes: {role: "A"}});
            writer.flush(); calls[0].reject({code: "network_error"});
        ''')
        self.assertEqual("saveFailed", self.evaluate("writer.status"))
        self.evaluate('writer.enqueue("edit", {item_id: "stable-item", changes: {role: "B"}})')
        self.assertEqual("B", self.evaluate("writer.items()[0].fields.role"))
        self.evaluate("writer.retry();")
        self.assertEqual("true", self.evaluate(
            "JSON.stringify(calls[0].body) === JSON.stringify(calls[1].body)",
        ))
        self.evaluate('calls[1].resolve(draft(2, "A"));')
        self.assertEqual("B", self.evaluate("writer.items()[0].fields.role"))
        self.evaluate('calls[2].resolve(draft(3, "B"));')
        self.assertEqual("saved", self.evaluate("writer.status"))
        self.assertEqual("3", self.evaluate("writer.draft.version"))

    def test_validation_correction_and_old_result_guard(self):
        self.evaluate('''
            writer.enqueue("edit", {item_id: "stable-item", changes: {role: ""}});
            writer.flush(); calls[0].reject({code: "validation_error"});
        ''')
        self.evaluate('''
            writer.enqueue("edit", {item_id: "stable-item", changes: {role: "corrected"}});
            writer.flush();
        ''')
        self.assertEqual("false", self.evaluate(
            "calls[0].body.idempotency_key === calls[1].body.idempotency_key",
        ))
        self.evaluate('writer.load(draft(5, "newer")); calls[1].resolve(draft(2, "old"));')
        self.assertEqual("5", self.evaluate("writer.draft.version"))
        self.assertEqual("newer", self.evaluate("writer.items()[0].fields.role"))

    def test_markdown_source_positions_and_escaping(self):
        self.evaluate((STATIC / "markdown.js").read_text().replace("export ", ""))
        self.assertEqual("true", self.evaluate('''
            const source = "# 😀\\r\\nRepeat **word**\\r\\n\\r\\nRepeat **word**";
            const html = renderMarkdown(source);
            html.includes('data-source-start="15">word') &&
              html.includes('data-source-start="34">word');
        '''))
        self.assertEqual("false", self.evaluate('''
            renderMarkdown('<script>alert(1)</script> [bad](javascript:alert)')
              .includes('<script>');
        '''))
        self.assertEqual("false", self.evaluate('''
            renderMarkdown('[bad](javascript:alert)').includes('href=');
        '''))
