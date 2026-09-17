-- Local UI preferences; independent of career facts and review state.
CREATE TABLE ui_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    language TEXT NOT NULL CHECK (language IN ('zh', 'en'))
);
INSERT INTO ui_settings (id, language) VALUES (1, 'zh');
