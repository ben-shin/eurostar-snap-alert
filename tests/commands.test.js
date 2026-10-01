const { test } = require("node:test");
const assert = require("node:assert/strict");
const { applyCommand, todayIn, handler } = require("../twilio/commands");
const now = new Date("2026-10-01T13:00:00Z");
const initial = () => ({version: 1, enabled: true, timezone: "Europe/London", next_id: 1, searches: [], processed: []});
const run = (state, command) => applyCommand(state, command, now);
const add = (state = initial()) => run(state, "SEARCH ADD from=Brussels to=London start=2026-10-02 end=2026-10-10").data;

test("start/stop are reversible and keep search parameters", () => {
  const state = add();
  const paused = run(state, "scan stop").data;
  assert.equal(paused.enabled, false);
  assert.deepEqual(paused.searches, state.searches);
  assert.equal(run(paused, "SCAN START").data.enabled, true);
  assert.equal(state.enabled, true);
});
test("add, edit, pause, resume and delete individual searches", () => {
  let state = add(add());
  assert.deepEqual(state.searches.map(s => s.id), ["s1", "s2"]);
  state = run(state, "SEARCH SET s1 from=Paris passengers=2 max=45.50 mode=both").data;
  assert.equal(state.searches[0].origin, "Paris Gare du Nord");
  assert.equal(state.searches[0].passengers, 2);
  assert.equal(state.searches[0].max, 45.5);
  assert.equal(state.searches[0].mode, "both");
  state = run(state, "SEARCH STOP s1").data;
  assert.equal(state.searches[0].enabled, false);
  assert.equal(state.searches[1].enabled, true);
  state = run(state, "SEARCH START s1").data;
  assert.equal(state.searches[0].enabled, true);
  state = run(state, "SEARCH DELETE s1").data;
  assert.deepEqual(state.searches.map(s => s.id), ["s2"]);
  assert.equal(add(state).searches[1].id, "s3");
});
test("expired search stays stopped until valid dates are supplied", () => {
  const state = add();
  state.searches[0].end_date = "2026-09-30";
  assert.match(run(state, "STATUS").reply, /expired/);
  assert.throws(() => run(state, "SEARCH START s1"), /expired/);
  assert.equal(run(state, "SEARCH SET s1 end=2026-10-12").data.searches[0].end_date, "2026-10-12");
  assert.equal(todayIn("Europe/London", new Date("2026-10-01T23:30:00Z")), "2026-10-02");
});
for (const bad of [
  "start=2026-02-30", "end=2026-01-01", "end=2027-10-01", "passengers=0",
  "passengers=5", "max=nan", "max=-1", "max=501", "mode=evil", "from=Moon",
  "to=Brussels", "url=https://bad.example", "end=2026-10-10 end=2026-10-11",
]) {
  test("reject invalid edit atomically: " + bad, () => {
    const state = add(), before = JSON.stringify(state);
    assert.throws(() => run(state, "SEARCH SET s1 " + bad));
    assert.equal(JSON.stringify(state), before);
  });
}
test("webhook checks owner, deduplicates retries and uses conditional writes", async () => {
  global.Twilio = {twiml: {MessagingResponse: class { constructor(){this.messages=[];} message(x){this.messages.push(x);} }}};
  let data = initial(), revision = 0, writes = 0, conflicts = 1;
  const document = {
    fetch: async () => ({data, revision: String(revision)}),
    update: async (args) => {
      if (conflicts-- > 0) throw {status: 412};
      assert.equal(args.ifMatch, String(revision));
      data = args.data; revision++; writes++;
    },
  };
  const context = {
    OWNER_WHATSAPP: "whatsapp:+440000000001", BOT_WHATSAPP: "whatsapp:+10000000000", SYNC_SERVICE_SID: "ISfake",
    getTwilioClient: () => ({sync: {v1: {services: () => ({documents: () => document})}}}),
  };
  const event = {From: context.OWNER_WHATSAPP, To: context.BOT_WHATSAPP, MessageSid: "SM" + "a".repeat(32), Body: "SCAN STOP"};
  const invoke = (e) => new Promise((resolve, reject) => handler(context, e, (err, value) => err ? reject(err) : resolve(value)));
  assert.equal((await invoke({...event, From: "whatsapp:+449999999999"})).messages.length, 0);
  assert.equal(writes, 0);
  assert.match((await invoke(event)).messages[0], /paused/);
  assert.equal(data.enabled, false);
  assert.equal((await invoke(event)).messages.length, 0);
  assert.equal(writes, 1);
});
