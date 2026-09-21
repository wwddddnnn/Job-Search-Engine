"""Execute the actual dependency-free editor logic in macOS's system JavaScriptCore.

No Node, packages or external network. Other platforms explicitly skip this engine harness.
"""

import ctypes
import json
from pathlib import Path
import re
import sys
import unittest

STATIC = Path(__file__).resolve().parents[1] / "src/job_search_assistant/adapters/web/static/js"


@unittest.skipUnless(sys.platform == "darwin", "System JavaScriptCore harness requires macOS")
class JavaScriptTests(unittest.TestCase):
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


class EditorLogicTests(JavaScriptTests):
    def setUp(self):
        super().setUp()
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
        self.assertEqual("1", self.evaluate("writer.queue.length"))
        self.assertEqual("corrected", self.evaluate("writer.items()[0].fields.role"))
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


class EditorActionViewTests(JavaScriptTests):
    def module(self, name):
        source = (STATIC / name).read_text()
        source = re.sub(r'import\s+.*?from\s+"[^"]+";\n', '', source, flags=re.S)
        self.evaluate(source.replace("export ", ""))

    def setUp(self):
        super().setUp()
        self.evaluate('''
            let serial = 0;
            const crypto = {randomUUID: () => String(++serial)};
            const setTimeout = () => 1, clearTimeout = () => {};
            const calls = [];
            function request(path, options = {}) {
              return new Promise((resolve, reject) => {
                calls.push({path, ...structuredClone(options), resolve, reject});
              });
            }
            const isStale = () => false;
        ''')
        for name in ("state.js", "i18n.js", "editor-state.js", "editor-actions.js"):
            self.module(name)
        self.evaluate((Path(__file__).parent / "web_dom_stub.js").read_text())
        self.module("views/editor.js")
        self.evaluate('''
            const initial = {id: "d", version: 1, base_version_id: null, items: []};
            loadEditor(initial);
            const renderEditor = editorView();
            subscribe(renderEditor);
            renderEditor(getState());
        ''')

    def test_actions_block_preview_and_publish_with_unsaved_input(self):
        self.evaluate('''
            compose("experience");
            update({preview: {draft_id: "d", draft_version: 1, base_version_id: null,
              content: [], summary: {added: [{fields: {role: "B"}}], modified: [], deleted: []}}});
            previewPublication(); publish();
        ''')
        self.assertEqual("0", self.evaluate("calls.length"))
        self.evaluate('''
            cancelComposer();
            writer.enqueue("edit", {item_id: "item", changes: {role: "pending"}});
            update({preview: {draft_id: "d", draft_version: 1, content: [],
              summary: {added: [{fields: {role: "B"}}], modified: [], deleted: []}}});
            previewPublication(); publish();
        ''')
        self.assertEqual("0", self.evaluate("calls.length"))
        self.assertEqual("true", self.evaluate('byKey("previewPublish").disabled'))

    def test_composer_locked_inputs_remain_copyable_and_can_collapse(self):
        for code in ("conflict", "authorization_error", "network_error"):
            with self.subTest(code=code):
                self.setUp()
                self.evaluate('''
                    compose("experience");
                    const roleInput = document.querySelector("#editor")
                      .querySelectorAll("input")[1];
                    roleInput.value = "retained"; roleInput.listeners.input();
                ''')
                self.evaluate('createItem();')
                self.evaluate(f'calls[0].reject({{code: "{code}"}});')
                self.assertEqual("true", self.evaluate(
                    'document.querySelector("#editor").querySelectorAll("input")'
                    '.every(input => input.readOnly && !input.disabled)'))
                self.assertEqual("false", self.evaluate('byKey("cancel").disabled'))
                self.evaluate('byKey("cancel").listeners.click();')
                self.assertEqual("true", self.evaluate('byKey("cancel").parentElement.hidden'))
                self.assertEqual("retained", self.evaluate('getState().composer.fields.role'))
                self.assertEqual("1", self.evaluate('writer.queue.length'))
                self.assertEqual("true", self.evaluate('byKey("previewPublish").disabled'))
                self.evaluate('byKey("resumeComposer").listeners.click();')
                self.assertEqual("false", self.evaluate('byKey("cancel").parentElement.hidden'))
                self.assertEqual("retained", self.evaluate(
                    'document.querySelector("#editor").querySelectorAll("input")[1].value'))

    def test_composer_pending_saving_and_publishing_gates(self):
        self.evaluate('compose("experience"); writer.enqueue("create", getState().composer);')
        for action in ('', 'writer.flush();'):
            self.evaluate(action or 'writer.emit();')
            self.assertEqual("true", self.evaluate(
                'document.querySelector("#editor").querySelectorAll("input")'
                '.every(input => input.readOnly && !input.disabled)'))
        self.evaluate('update({publishing: true});')
        self.assertEqual("true", self.evaluate(
            'document.querySelector("#editor").querySelectorAll("input")'
            '.every(input => input.disabled)'))
        self.assertEqual("true", self.evaluate('byKey("cancel").disabled'))
        self.evaluate('cancelComposer();')
        self.assertEqual("false", self.evaluate('byKey("cancel").parentElement.hidden'))

    def test_composer_validation_correction_uses_new_key(self):
        self.evaluate('''
            compose("experience"); createItem();
            calls[0].reject({code: "validation_error"});
        ''')
        self.assertEqual("true", self.evaluate(
            'document.querySelector("#editor").querySelectorAll("input")'
            '.every(input => !input.readOnly && !input.disabled)'))
        self.evaluate('changeComposer({organization: "A", role: "corrected"}); createItem();')
        self.assertEqual("false", self.evaluate(
            "calls[0].body.idempotency_key === calls[1].body.idempotency_key"))
        self.assertEqual("corrected", self.evaluate("calls[1].body.fields.role"))
        self.assertEqual("1", self.evaluate("writer.queue.length"))
        self.assertEqual("corrected", self.evaluate("getState().composer.fields.role"))

    def test_conflict_copy_refresh_and_resave_unlocks_publication(self):
        self.evaluate('''
            compose("experience");
            const inputs = document.querySelector("#editor").querySelectorAll("input");
            inputs[0].value = "retained company"; inputs[0].listeners.input();
            const roleInput = inputs[1];
            roleInput.value = "retained role"; roleInput.listeners.input(); createItem();
            calls[0].reject({code: "conflict"});
        ''')
        self.assertEqual("false", self.evaluate("roleInput.disabled"))
        self.assertEqual("true", self.evaluate("roleInput.readOnly"))
        self.assertEqual("retained role", self.evaluate("roleInput.value"))
        self.assertEqual("true", self.evaluate('byKey("retrySave").hidden'))
        self.assertEqual("true", self.evaluate('byKey("previewPublish").disabled'))
        copied = self.evaluate("JSON.stringify(getState().composer.fields)")
        failed_key = self.evaluate("calls[0].body.idempotency_key")
        # Refresh starts a genuinely new JS context; only manually copied text survives.
        self.setUp()
        self.evaluate("serial = 100; loadEditor({...initial, version: 2});")
        self.evaluate(f"const copiedFields = {copied};")
        self.evaluate('compose("experience"); changeComposer(copiedFields); createItem();')
        self.assertEqual("2", self.evaluate("calls[0].body.expected_version"))
        self.assertEqual("retained role", self.evaluate("calls[0].body.fields.role"))
        self.assertNotEqual(failed_key, self.evaluate("calls[0].body.idempotency_key"))
        self.evaluate('''
            calls[0].resolve({...initial, version: 3, items: [{id: "item", kind: "experience",
              fields: copiedFields, status: "draft", sources: []}]});
        ''')
        self.assertEqual("saved", self.evaluate("writer.status"))
        self.assertEqual("null", self.evaluate("getState().composer"))
        self.assertEqual("false", self.evaluate('byKey("previewPublish").disabled'))
        self.evaluate("previewPublication();")
        self.assertEqual("/review/preview", self.evaluate("calls[1].path"))

    def test_publish_retry_reuses_identical_command(self):
        self.evaluate('''
            update({preview: {draft_id: "d", draft_version: 1, base_version_id: null,
              content: [], summary: {added: [{fields: {role: "B"}}], modified: [], deleted: []}}});
            publish(); calls[0].reject({code: "network_error"});
        ''')
        self.evaluate("publish();")
        self.assertEqual("true", self.evaluate(
            "JSON.stringify(calls[0].body) === JSON.stringify(calls[1].body)"))
        self.evaluate('calls[1].resolve({draft: {...initial, version: 2}});')
        self.evaluate('calls[2].resolve({version: 1});')
        self.evaluate('calls[3].resolve([]);')
        self.evaluate('calls[4].resolve({display_name: "Profile", version: 1, experiences: []});')
        self.assertEqual("false", self.evaluate("getState().publishing"))
        self.assertEqual("null", self.evaluate("getState().preview"))

    def test_edit_confirmed_item_renders_awaiting_confirmation(self):
        self.evaluate('''
            loadEditor({...initial, items: [{id: "item", kind: "experience", status: "verified",
              fields: {organization: "A", role: "B"}, sources: []}]});
        ''')
        self.assertIn("已确认", self.evaluate('textOf(document.querySelector("#editor"))'))
        self.evaluate('editItem("item", {role: "edited"});')
        rendered = self.evaluate('textOf(document.querySelector("#editor"))')
        self.assertIn("待核验", rendered)
        self.assertNotIn("已确认", rendered)
        self.assertEqual("true", self.evaluate('byKey("confirm").disabled'))
        self.evaluate('''
            writer.flush(); calls[0].resolve({...initial, version: 2, items: writer.items()});
        ''')
        self.assertEqual("false", self.evaluate('byKey("confirm").disabled'))
        self.assertIn("待核验", self.evaluate('textOf(document.querySelector("#editor"))'))

    def test_history_click_renders_old_snapshot_without_changing_profile(self):
        self.evaluate('''
            const currentProfile = {display_name: "Current", version: 2, experiences: []};
            update({profile: currentProfile, snapshot: currentProfile,
              versions: [{id: "old", version: 1, created_at: "then"}]});
            document.querySelector("#editor").querySelectorAll("button")
              .find(node => node.textContent.startsWith("v1")).listeners.click();
            calls[0].resolve([{id: "old", version: 1, created_at: "then"}]);
        ''')
        self.assertEqual("/profile/versions/old", self.evaluate("calls[1].path"))
        self.evaluate('''
            calls[1].resolve({display_name: "Old snapshot", version: 1, experiences: [{
              organization: "Old company", role: "Old role", summary: "Old summary",
              achievements: [{action_text: "Old achievement"}], skills: [{name: "Old skill"}],
            }]});
        ''')
        rendered = self.evaluate('textOf(document.querySelector("#editor"))')
        for expected in ("Old snapshot · v1", "Old company · Old role", "Old summary",
                         "Old achievement", "Old skill"):
            self.assertIn(expected, rendered)
        self.assertEqual("true", self.evaluate("getState().profile === currentProfile"))
        self.assertEqual("true", self.evaluate("calls.every(call => !call.method)"))

    def test_metric_conversion_and_unknown_translation_fallback(self):
        for value, expected in (("", "null"), ("invalid", "null"), ("12.5", "12.5")):
            with self.subTest(value=value):
                self.assertEqual(expected, self.evaluate(
                    'JSON.stringify(values({metric_value: {value: '
                    + json.dumps(value) + '}}).metric_value)'))
        for language, expected in (("zh", "操作失败，请重试。"),
                                   ("en", "Operation failed. Please try again.")):
            self.assertEqual(expected, self.evaluate(f't("{language}", "unrecognized_code")'))

    def test_reader_converts_utf16_selection_to_codepoints(self):
        self.module("markdown.js")
        self.module("views/reader.js")
        self.evaluate('''
            readerView(() => {});
            update({view: "source", document: {id: "doc", content: "😀A🚀B"}});
            const sourceRoot = document.querySelector("#source");
            const textNode = {length: 6}; sourceRoot.children = [textNode];
            sourceRoot.textNodes = [textNode];
            selectedRange = {commonAncestorContainer: sourceRoot, collapsed: false,
              startContainer: textNode, endContainer: textNode, startOffset: 2, endOffset: 5,
              intersectsNode: () => true};
            document.listeners.selectionchange();
        ''')
        self.assertEqual('A🚀', self.evaluate('getState().selection.text'))
        self.assertEqual('1', self.evaluate('getState().selection.locator.start'))
        self.assertEqual('3', self.evaluate('getState().selection.locator.end'))

    def test_three_empty_state_rendering_branches(self):
        # Load the real app renderer with side-effect-only app dependencies stubbed.
        self.evaluate('''
            const initialize = () => {}, selectDocument = () => {}, changeView = () => {};
            const changeLanguage = () => {}, openDraft = () => {}, importFile = () => {};
            const retryImport = () => {}, canRetryImport = () => false;
            const documentList = () => () => {}, readerView = () => () => {};
            const llmView = () => () => {};
            class APIError { localized() { return "error"; } }
        ''')
        self.module("views/app.js")
        for language in ("zh", "en"):
            self.evaluate(f'update({{language: "{language}", draft: null, profile: null}});')
            self.assertEqual(self.evaluate(f't("{language}", "noProfile")'),
                             self.evaluate('document.querySelector("#profile-status").textContent'))
            self.assertEqual(self.evaluate(f't("{language}", "noDraft")'),
                             self.evaluate('document.querySelector("#draft-status").textContent'))
            self.evaluate('loadEditor(initial); update({profile: {version: 0}});')
            self.assertEqual(self.evaluate(f't("{language}", "noProfile")'),
                             self.evaluate('document.querySelector("#profile-status").textContent'))
            self.assertIn(self.evaluate(f't("{language}", "manualHint")'),
                          self.evaluate('textOf(document.querySelector("#editor"))'))
            self.assertEqual("false", self.evaluate('document.querySelector("#editor").hidden'))
