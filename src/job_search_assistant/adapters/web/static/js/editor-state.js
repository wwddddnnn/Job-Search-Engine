// Transport-independent ordered writer. Failed requests retain their exact idempotency key.
export class SaveQueue {
  constructor(send, notify, key = () => crypto.randomUUID()) {
    this.send = send; this.notify = notify; this.key = key;
    this.draft = null; this.queue = []; this.running = false; this.failure = null;
    this.status = "unsaved";
  }
  load(draft) { this.draft = draft; this.status = draft ? "saved" : "unsaved"; this.emit(); }
  emit() { this.notify(this); }
  enqueue(action, payload) {
    // Validation made no write: discard only the rejected edit, retain the corrected payload,
    // and allocate a new idempotency key because its input differs from the failed command.
    if (this.failure?.code === "validation_error" && action === "edit" &&
        this.queue[0]?.payload.item_id === payload.item_id) {
      this.queue.shift(); this.failure = null;
    }
    const tail = this.queue.at(-1);
    if (action === "edit" && tail?.action === "edit" && !tail.body &&
        tail.payload.item_id === payload.item_id) tail.payload = structuredClone(payload);
    else this.queue.push({action, payload: structuredClone(payload), key: this.key()});
    this.status = this.failure ? "saveFailed" : "pending"; this.emit();
  }
  async flush() {
    if (this.running || this.failure || !this.queue.length) return;
    this.running = true;
    while (this.queue.length) {
      const entry = this.queue[0];
      entry.body ??= {...entry.payload, draft_id: this.draft.id,
        expected_version: this.draft.version, idempotency_key: entry.key};
      this.status = "saving"; this.emit();
      try {
        const result = await this.send(entry.action, entry.body);
        // A replay may acknowledge an older save. Never roll the saved baseline backwards.
        const draft = result.draft || result;
        if (draft.id === this.draft.id && draft.version >= this.draft.version) this.draft = draft;
        this.queue.shift();
      } catch (error) {
        this.failure = error; this.status = "saveFailed"; break;
      }
    }
    this.running = false;
    if (!this.failure) this.status = this.queue.length ? "pending" : "saved";
    this.emit();
  }
  retry() { this.failure = null; return this.flush(); }
  get clean() { return !this.running && !this.queue.length && !this.failure; }
  items() {
    const items = structuredClone(this.draft?.items || []);
    for (const entry of this.queue) {
      if (entry.action !== "edit") continue;
      const item = items.find(item => item.id === entry.payload.item_id);
      if (item) { Object.assign(item.fields, entry.payload.changes); item.status = "draft"; }
    }
    return items;
  }
}
