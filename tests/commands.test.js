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

const { applyInput } = require("../twilio/commands");
const owner = "whatsapp:+440000000001";
function journey(state = initial(), at = now) {
  const replies = [];
  return {
    get data() { return state; },
    get replies() { return replies; },
    say(message) {
      const result = applyInput(state, message, owner, at);
      state = result.data;
      replies.push(result.reply);
      return result.reply;
    }
  };
}
test("guided add collects one answer at a time and saves only after confirmation", () => {
  const chat = journey();
  assert.match(chat.say("Hi"), /1 Add a search/);
  assert.match(chat.say("1"), /leave from/);
  assert.match(chat.say("Moon"), /Choose a city/);
  assert.match(chat.say("2"), /going/);
  assert.match(chat.say("1"), /First travel date/);
  assert.match(chat.say("02\/10\/2026"), /Last travel date/);
  chat.say("10/10/2026");
  chat.say("2");
  assert.match(chat.say("3"), /Highest normal fare/);
  assert.match(chat.say("45.50"), /Add this search/);
  assert.equal(chat.data.searches.length, 0);
  assert.match(chat.say("perhaps"), /YES or NO/);
  assert.equal(chat.data.searches.length, 0);
  assert.match(chat.say("yes"), /Added your search/);
  assert.equal(chat.data.searches[0].origin, "Brussels Midi");
  assert.equal(chat.data.searches[0].destination, "London St Pancras");
  assert.equal(chat.data.searches[0].start_date, "2026-10-02");
  assert.equal(chat.data.searches[0].passengers, 2);
  assert.equal(chat.data.searches[0].mode, "both");
  assert.equal(chat.data.searches[0].max, 45.5);
  assert.equal(chat.data.conversations[owner], undefined);
});
test("guided back and cancel discard a draft; expired session restarts helpfully", () => {
  const chat = journey();
  chat.say("1");
  chat.say("2");
  assert.match(chat.say("back"), /leave from/);
  chat.say("1");
  assert.match(chat.say("cancel"), /Cancelled/);
  assert.equal(chat.data.searches.length, 0);
  const expired = applyInput(chat.data, "1", owner, now).data;
  assert.equal(expired.conversations[owner].stage, "add");
  const later = new Date(now.getTime() + 31 * 60 * 1000);
  const result = applyInput(expired, "Brussels", owner, later);
  assert.match(result.reply, /I didn't catch that/);
  assert.equal(result.data.conversations[owner], undefined);
});
test("guided edit and delete require confirmation; invalid edit does not change a search", () => {
  const chat = journey(add());
  const before = JSON.stringify(chat.data.searches);
  chat.say("2"); chat.say("1"); chat.say("1"); chat.say("4");
  assert.match(chat.say("30/02/2027"), /real date/);
  assert.equal(JSON.stringify(chat.data.searches), before);
  assert.match(chat.say("12/10/2026"), /Save this change/);
  assert.equal(JSON.stringify(chat.data.searches), before);
  assert.match(chat.say("no"), /No changes made/);
  assert.equal(JSON.stringify(chat.data.searches), before);
  chat.say("2"); chat.say("1"); chat.say("1"); chat.say("4");
  chat.say("12/10/2026"); chat.say("yes");
  assert.equal(chat.data.searches[0].end_date, "2026-10-12");
  chat.say("2"); chat.say("1");
  assert.match(chat.say("2"), /paused/);
  assert.equal(chat.data.searches[0].enabled, false);
  chat.say("2"); chat.say("1");
  assert.match(chat.say("2"), /resumed/);
  chat.say("2"); chat.say("1"); chat.say("3");
  assert.match(chat.say("back"), /Delete/);
  chat.say("3");
  assert.match(chat.say("no"), /No changes made/);
  assert.equal(chat.data.searches.length, 1);
  chat.say("2"); chat.say("1"); chat.say("3"); chat.say("yes");
  assert.equal(chat.data.searches.length, 0);
});
test("guided expired search cannot resume until its dates are edited", () => {
  const state = add();
  state.searches[0].end_date = "2026-09-30";
  state.searches[0].enabled = false;
  const chat = journey(state);
  chat.say("2"); chat.say("1");
  assert.match(chat.say("2"), /expired/);
  assert.equal(chat.data.searches[0].enabled, false);
  chat.say("1"); chat.say("4"); chat.say("10/10/2026");
  assert.match(chat.say("yes"), /Saved your search/);
  chat.say("2"); chat.say("1");
  assert.match(chat.say("2"), /resumed/);
});
test("legacy commands still work and interrupt an unfinished conversation", () => {
  const chat = journey();
  chat.say("1");
  assert.match(chat.say("SEARCH ADD from=Brussels to=London start=2026-10-02 end=2026-10-10"), /Added s1/);
  assert.equal(chat.data.searches.length, 1);
  assert.equal(chat.data.conversations[owner], undefined);
  assert.match(chat.say("SCAN STOP"), /paused/);
  assert.match(chat.say("STATUS"), /Scanning OFF/);
  assert.match(chat.say("HELP"), /Add a search/);
  assert.match(chat.say("COMMANDS"), /SEARCH ADD/);
  assert.match(chat.say("My searches"), /Which search/);
});

const sid = (letter) => "SM" + letter.repeat(32);
function sampleReport(state, count = 3) {
  const search = state.searches[0];
  const hit = (date, price) => ({
    search_id: search.id, provider: "normal_eurostar", route: search.name,
    date, price, currency: "GBP", departure_time: "07:56", arrival_time: "08:57",
    duration: "2h 01m", fare_class: "Eurostar Standard",
    booking_url: "https://www.eurostar.com/search/uk-en?outbound=" + date,
  });
  state.check_report = {
    request_id: null, started_at: "2026-10-01T11:50:00Z",
    completed_at: "2026-10-01T12:00:00Z", state: "complete",
    attempted: count, completed: count, failed: 0,
    hit_count: count, omitted_hits: 0,
    searches: [{id: search.id, status: "active", settings: {...search},
      attempted: count, completed: count, failed: 0, hit_count: count}],
    hits: [hit("2026-10-03", 45), hit("2026-10-04", 50), hit("2026-10-05", 55)].slice(0, count),
    errors: [],
  };
  return state;
}
test("check now queues one request without resuming paused automatic alerts", () => {
  const state = add();
  state.enabled = false;
  const first = applyInput(state, "CHECK NOW", owner, now, sid("b"));
  assert.equal(first.data.enabled, false);
  assert.deepEqual(first.data.searches, state.searches);
  assert.equal(first.data.check_request.id, sid("b"));
  assert.equal(first.data.check_request.status, "queued");
  assert.equal(first.checkQueued, true);
  assert.match(first.reply, /Automatic alerts remain paused/);
  const again = applyInput(first.data, "6", owner, now, sid("c"));
  assert.equal(again.checkQueued, undefined);
  assert.equal(again.data.check_request.id, sid("b"));
  assert.match(again.reply, /already queued/);
});
test("check now with no enabled search queues nothing; stop and cancel remove requests", () => {
  let state = initial();
  assert.match(applyInput(state, "check now", owner, now, sid("b")).reply, /No enabled/);
  assert.equal(state.check_request, undefined);
  state = add();
  state.searches[0].enabled = false;
  assert.equal(applyInput(state, "check now", owner, now, sid("b")).data.check_request, undefined);
  state.searches[0].enabled = true;
  state = applyInput(state, "check now", owner, now, sid("b")).data;
  const cancelled = applyInput(state, "CANCEL", owner, now, sid("c"));
  assert.match(cancelled.reply, /Cancelled the one-off check/);
  assert.equal(cancelled.data.check_request, undefined);
  state = applyInput(cancelled.data, "check now", owner, now, sid("d")).data;
  state = applyInput(state, "SCAN STOP", owner, now, sid("e")).data;
  assert.equal(state.check_request, undefined);
  state = applyInput(state, "check now", owner, now, sid("f")).data;
  state = applyInput(state, "3", owner, now, sid("a")).data;
  assert.equal(state.check_request, undefined);
});
test("latest report shows matched journey, timestamp, status and natural MORE pages", () => {
  const state = sampleReport(add());
  let result = applyInput(state, "7", owner, now);
  assert.match(result.reply, /Latest fare results/);
  assert.match(result.reply, /1 Oct 2026/);
  assert.match(result.reply, /Showing 1-2 of 3 saved matches/);
  assert.match(result.reply, /£45/);
  assert.match(result.reply, /Eurostar Standard/);
  assert.match(result.reply, /Dep 07:56 \/ Arr 08:57/);
  assert.match(result.reply, /https:\/\/www.eurostar.com/);
  assert.match(result.reply, /Reply MORE/);
  result = applyInput(result.data, "MORE", owner, now);
  assert.match(result.reply, /Showing 3-3 of 3 saved matches/);
  assert.match(result.reply, /£55/);
  assert.doesNotMatch(result.reply, /£45/);
  assert.match(applyInput(result.data, "MORE", owner, now).reply, /all the saved matches/);
  const settings = applyInput(state, "STATUS", owner, now);
  assert.match(settings.reply, /Scanning ON/);
  assert.match(settings.extraReply, /Latest fare results/);
});
test("changed, paused and expired searches cannot masquerade as current hits", () => {
  let state = sampleReport(add(), 1);
  state.searches[0].max = 20;
  assert.match(applyInput(state, "RESULTS", owner, now).reply, /no longer fit current searches/);
  assert.match(applyInput(state, "RESULTS", owner, now).reply, /coverage is incomplete/);
  state = sampleReport(add(), 1);
  state.enabled = false;
  assert.match(applyInput(state, "RESULTS", owner, now).reply, /Cached results while automatic alerts are paused/);
  state.searches[0].end_date = "2026-09-30";
  assert.match(applyInput(state, "RESULTS", owner, now).reply, /expired/);
  assert.match(applyInput(state, "RESULTS", owner, now).reply, /coverage is incomplete/);
});
test("partial results and storage omissions are disclosed rather than called clear", () => {
  const state = sampleReport(add(), 0);
  state.check_report.state = "partial";
  state.check_report.attempted = 2;
  state.check_report.completed = 1;
  state.check_report.failed = 1;
  state.check_report.hit_count = 4;
  state.check_report.omitted_hits = 4;
  const reply = applyInput(state, "RESULTS", owner, now).reply;
  assert.match(reply, /Partial: 1\/2 checks completed; 1 failed/);
  assert.match(reply, /No confirmed matches shown here/);
  assert.match(reply, /4 more match\(es\) omitted/);
});
test("webhook queues one request, acknowledges schedule, and deduplicates retries", async () => {
  global.Twilio = {twiml: {MessagingResponse: class {
    constructor(){this.messages=[];} message(x){this.messages.push(x);}
  }}};
  let data = add(), revision = 0, writes = 0;
  const document = {
    fetch: async () => ({data, revision: String(revision)}),
    update: async ({data: next, ifMatch}) => {
      assert.equal(ifMatch, String(revision));
      data = next; revision++; writes++;
    },
  };
  const context = {
    OWNER_WHATSAPP: owner, BOT_WHATSAPP: "whatsapp:+10000000000", SYNC_SERVICE_SID: "ISfake",
    getTwilioClient: () => ({sync:{v1:{services: () => ({documents: () => document})}}}),
  };
  const invoke = (e) => new Promise((resolve, reject) =>
    handler(context, e, (err, value) => err ? reject(err) : resolve(value)));
  const event = {From: owner, To: context.BOT_WHATSAPP, MessageSid: sid("b"), Body: "CHECK NOW"};
  const reply = await invoke(event);
  assert.match(reply.messages[0], /next scheduled scan can take about 15 minutes/);
  assert.match(reply.messages[1], /Latest fare results/);
  assert.equal(data.check_request.id, sid("b"));
  assert.equal(writes, 1);
  assert.equal((await invoke(event)).messages.length, 0);
  assert.equal(writes, 1);
});

test("zero-hit report becomes inapplicable after editing the fare limit", () => {
  const state = sampleReport(add(), 0);
  state.check_report.attempted = 1;
  state.check_report.completed = 1;
  state.searches[0].max = 200;
  const reply = applyInput(state, "RESULTS", owner, now).reply;
  assert.match(reply, /Search s1 changed since report/);
  assert.match(reply, /current settings were not checked/);
  assert.match(reply, /coverage is incomplete/);
  assert.doesNotMatch(reply, /No current matches in this report/);
});
test("zero-hit report identifies a search added after the snapshot", () => {
  const state = sampleReport(add(), 0);
  state.check_report.attempted = 1;
  state.check_report.completed = 1;
  const updated = add(state);
  const reply = applyInput(updated, "RESULTS", owner, now).reply;
  assert.match(reply, /Search s2 added since report; not yet checked/);
  assert.match(reply, /coverage is incomplete/);
  assert.doesNotMatch(reply, /No current matches in this report/);
});
test("mixed success lists failed route and date without exposing backend error text", () => {
  const state = sampleReport(add(), 1);
  state.check_report.state = "partial";
  state.check_report.attempted = 4;
  state.check_report.completed = 1;
  state.check_report.failed = 3;
  state.check_report.omitted_errors = 2;
  state.check_report.errors = [
    {search_id:"s1",provider:"normal_eurostar",date:"2026-10-06",message:"secret token abc"},
    {search_id:"s1",provider:"snap",date:"2026-10-07",message:"private debug stack"},
    {search_id:"s1",provider:"snap",date:"2026-10-08",message:"internal URL"},
  ];
  let result = applyInput(state, "RESULTS", owner, now);
  assert.match(result.reply, /2026-10-06: check failed/);
  assert.match(result.reply, /2026-10-07: check failed/);
  assert.match(result.reply, /2 more failed check\(s\) omitted/);
  assert.doesNotMatch(result.reply, /secret token|private debug|internal URL/);
  assert.match(result.reply, /Reply MORE/);
  assert.ok(result.reply.length <= 1500);
  result = applyInput(result.data, "MORE", owner, now);
  assert.match(result.reply, /2026-10-08: check failed/);
  assert.doesNotMatch(result.reply, /secret token|private debug|internal URL/);
});
test("cancel suppresses unsent reports and warns for already in-flight sends", () => {
  for (const status of ["report_ready", "sending", "delivery_pending"]) {
    const state = add();
    state.check_request = {id:sid("b"),requested_at:now.toISOString(),status,
      attempt:{body:"exact report"},pending_sid:sid("d")};
    const queued = applyInput(state, "CHECK NOW", owner, now, sid("c"));
    assert.match(queued.reply, /awaiting delivery/);
    assert.equal(queued.checkQueued, undefined);
    if (status === "sending") assert.match(queued.extraReply, /will not be resent while delivery is uncertain/);
    const cancelled = applyInput(state, "CANCEL", owner, now);
    assert.equal(cancelled.data.check_request.status, "cancelled");
    assert.equal(cancelled.data.check_request.cancelled_after, status);
    assert.equal(cancelled.data.check_request.attempt.body, "exact report");
    assert.equal(cancelled.data.check_request.pending_sid, sid("d"));
    assert.match(cancelled.reply, status === "report_ready" ? /unsent report/ : /already in-flight may still arrive/);
    assert.equal(applyInput(state, "SCAN STOP", owner, now).data.check_request.status, "cancelled");
  }
});
