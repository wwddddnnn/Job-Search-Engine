"""Run S3a state/actions/views using the existing macOS JavaScriptCore harness."""

import json
import re

import test_web_editor


class LLMViewTests(test_web_editor.JavaScriptTests):
    def module(self, name):
        source = (test_web_editor.STATIC / name).read_text()
        source = re.sub(r'import\s+.*?from\s+"[^"]+";\n', '', source, flags=re.S)
        self.evaluate(source.replace("export ", ""))

    def setUp(self):
        super().setUp()
        self.evaluate((test_web_editor.STATIC.parents[5] / "tests/web_dom_stub.js").read_text())
        self.evaluate('''
            let serial = 0;
            const crypto = {randomUUID: () => String(++serial)};
            const calls = [];
            function request(path, options = {}) {
              return new Promise((resolve, reject) => {
                calls.push({path, ...structuredClone(options), resolve, reject});
              });
            }
        ''')
        for name in ("llm-state.js", "llm-actions.js", "i18n.js", "views/llm.js"):
            self.module(name)
        self.evaluate('''
            const view = llmView(), root = document.querySelector("#llm-configs");
            const click = code => root.querySelectorAll("button")
              .find(node => node.dataset.llmCode === code).listeners.click();
            const config = {id: "one", name: "First", model: "Model",
              api_url: "https://example.invalid", has_key: true};
            updateLLM({configs: [config], selected: "one", loaded: true});
            view({language: "zh"});
        ''')

    def test_mask_is_not_input_or_submitted_key_and_bilingual_notice(self):
        self.assertIn("真实调用未接入", self.evaluate("textOf(root)"))
        self.evaluate('view({language: "en"});')
        self.assertIn("does not verify connectivity", self.evaluate("textOf(root)"))
        self.assertIn("Current configuration", self.evaluate("textOf(root)"))
        self.evaluate('click("llmEdit");')
        self.assertEqual("", self.evaluate('root.querySelectorAll("input")[3].value'))
        self.assertEqual("true", self.evaluate(
            'root.querySelectorAll("input")[3].parentElement.hidden'))
        self.evaluate('root.querySelectorAll("input")[0].value = "Changed"; click("llmSave");')
        body = json.loads(self.evaluate("JSON.stringify(calls[0].body)"))
        self.assertNotIn("api_key", body)
        self.assertEqual("Changed", body["name"])
        self.assertEqual("PUT", self.evaluate("calls[0].method"))
        self.evaluate('calls[0].reject({code: "network_error"});')
        self.evaluate('click("llmRetry");')
        self.assertEqual("true", self.evaluate(
            "JSON.stringify(calls[0].body) === JSON.stringify(calls[1].body)"))

    def test_reset_clear_create_delete_switch_and_no_state_secret(self):
        self.evaluate('click("llmEdit"); click("llmReset");')
        self.assertEqual("false", self.evaluate(
            'root.querySelectorAll("input")[3].parentElement.hidden'))
        self.evaluate('click("llmSave");')
        self.assertEqual("", self.evaluate("calls[0].body.api_key"))
        self.evaluate('calls[0].reject({code: "validation_error"}); click("cancel");')
        self.evaluate('click("llmAdd");')
        self.evaluate('''
            root.querySelectorAll("input")[3].value = "ephemeral-test-value";
            click("llmSave");
        ''')
        self.assertEqual("POST", self.evaluate("calls[1].method"))
        self.assertEqual("ephemeral-test-value", self.evaluate(
            'root.querySelectorAll("input")[3].value'))
        self.assertNotIn("ephemeral-test-value", self.evaluate("JSON.stringify(getLLMState())"))
        self.evaluate('calls[1].reject({code: "validation_error"}); click("cancel");')
        self.evaluate('click("llmSelect");')
        self.assertEqual("one", self.evaluate("calls[2].body.config_id"))
        self.evaluate('calls[2].reject({code: "validation_error"});')
        self.evaluate('click("delete");')
        self.assertEqual("DELETE", self.evaluate("calls[3].method"))
        self.assertEqual("/llm/configs/one", self.evaluate("calls[3].path"))

    def test_validation_retry_preserves_replacement_until_success(self):
        self.evaluate('''
            click("llmEdit"); click("llmReset");
            root.querySelectorAll("input")[3].value = "ephemeral-replacement";
            click("llmSave");
            calls[0].reject({code: "validation_error"});
        ''')
        self.evaluate('root.querySelectorAll("input")[0].value = "Fixed"; click("llmSave");')
        self.assertEqual("ephemeral-replacement", self.evaluate("calls[1].body.api_key"))
        self.assertNotEqual(self.evaluate("calls[0].body.idempotency_key"),
                            self.evaluate("calls[1].body.idempotency_key"))
        self.evaluate('calls[1].resolve(config);')
        self.assertEqual("", self.evaluate('root.querySelectorAll("input")[3].value'))
