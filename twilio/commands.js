"use strict";

// Deployed as a protected Twilio Function: Twilio validates the webhook signature.
const STATIONS = {
  london: "London St Pancras", brussels: "Brussels Midi", paris: "Paris Gare du Nord",
  amsterdam: "Amsterdam Centraal", rotterdam: "Rotterdam Centraal", lille: "Lille Europe",
};
const HELP = [
  "SCAN START / SCAN STOP",
  "STATUS / SEARCH LIST / TEST",
  "SEARCH ADD from=Brussels to=London start=2026-10-05 end=2026-10-10",
  "SEARCH SET s1 start=2026-10-06 end=2026-10-12 passengers=2 max=60 mode=snap",
  "SEARCH STOP s1 / SEARCH START s1 / SEARCH DELETE s1",
  "Cities: London, Brussels, Paris, Amsterdam, Rotterdam, Lille.",
  "mode=snap|normal|both; max is the normal fare limit in GBP or EUR.",
  "Commands reply now. Scans run on the GitHub schedule (about 15 min, possibly delayed).",
  "Use SCAN STOP, not bare STOP (which leaves the WhatsApp Sandbox).",
].join("\n");

function todayIn(timezone, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(now);
  const value = (type) => parts.find((p) => p.type === type).value;
  return [value("year"), value("month"), value("day")].join("-");
}
function validDate(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !Number.isNaN(Date.parse(value + "T00:00:00Z")) &&
    new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
}
function status(search, today) {
  return search.end_date < today ? "expired" : search.enabled ? "active" : "paused";
}
function describe(search, today) {
  return [
    search.id + " [" + status(search, today) + "] " + search.origin + " → " + search.destination,
    search.start_date + " to " + search.end_date + "; " + search.passengers + " passenger(s)",
    "mode=" + search.mode + "; max=" + search.max,
  ].join("\n");
}
function parseFields(tokens) {
  const fields = {};
  for (const token of tokens) {
    const match = /^([a-z]+)=(.+)$/i.exec(token);
    if (!match) throw new Error("Use key=value fields; send HELP for examples.");
    const key = match[1].toLowerCase();
    if (!["from", "to", "start", "end", "passengers", "max", "mode"].includes(key))
      throw new Error("Unknown field: " + key);
    if (Object.hasOwn(fields, key)) throw new Error("Repeated field: " + key);
    fields[key] = match[2];
  }
  return fields;
}
function editSearch(search, fields, today) {
  for (const [key, value] of Object.entries(fields)) {
    if (key === "from" || key === "to") {
      const station = STATIONS[value.toLowerCase()];
      if (!station) throw new Error("Unknown city. Use: " + Object.keys(STATIONS).join(", "));
      search[key === "from" ? "origin" : "destination"] = station;
    } else if (key === "start" || key === "end") {
      if (!validDate(value)) throw new Error("Dates must be real dates in YYYY-MM-DD format.");
      search[key + "_date"] = value;
    } else if (key === "passengers") {
      if (!/^[1-4]$/.test(value)) throw new Error("Passengers must be 1 to 4.");
      search.passengers = Number(value);
    } else if (key === "max") {
      if (!/^\d+(\.\d{1,2})?$/.test(value) || Number(value) <= 0 || Number(value) > 500)
        throw new Error("max must be a price above 0 and at most 500.");
      search.max = Number(value);
    } else {
      if (!["snap", "normal", "both"].includes(value.toLowerCase()))
        throw new Error("mode must be snap, normal or both.");
      search.mode = value.toLowerCase();
    }
  }
  if (!search.origin || !search.destination || !search.start_date || !search.end_date)
    throw new Error("Supply from, to, start and end.");
  if (search.origin === search.destination) throw new Error("Choose two different cities.");
  if (search.end_date < search.start_date) throw new Error("end must be on or after start.");
  if (search.end_date < today) throw new Error("That search has already expired. Set a future end date.");
  if ((Date.parse(search.end_date) - Date.parse(search.start_date)) / 86400000 > 60)
    throw new Error("Use a date window of at most 61 days.");
  search.name = search.origin + " to " + search.destination;
  return search;
}
function applyCommand(original, body, now = new Date()) {
  const data = JSON.parse(JSON.stringify(original));
  if (data.version !== 1 || !Array.isArray(data.searches)) throw new Error("Invalid control document.");
  const today = todayIn(data.timezone, now);
  const words = String(body || "").trim().split(/\s+/);
  const command = words.shift().toUpperCase();
  if (command === "HELP") return { data, reply: HELP, changed: false };
  if (command === "TEST") return { data, reply: "Eurostar bot is working. Your phone command reached the webhook. Send STATUS to see your searches.", changed: false };
  if (command === "STATUS" || (command === "SEARCH" && words[0]?.toUpperCase() === "LIST")) {
    const header = "Scanning " + (data.enabled ? "ON" : "OFF") +
      ". Expired searches never scan. Dates use " + data.timezone + ".";
    return { data, reply: header + "\n" + (data.searches.map((s) => describe(s, today)).join("\n\n") || "No searches. Send HELP."), changed: false };
  }
  if (command === "SCAN" && words.length === 1 && ["START", "STOP"].includes(words[0].toUpperCase())) {
    data.enabled = words[0].toUpperCase() === "START";
    return { data, changed: true, reply: data.enabled ?
      "Scanning enabled. Active, unexpired searches resume on the next scheduled run." :
      "Scanning and fare alerts paused. An in-flight page or message may finish. Send SCAN START to resume." };
  }
  if (command !== "SEARCH") throw new Error("Unknown command. Send HELP. To pause, use SCAN STOP.");
  const action = (words.shift() || "").toUpperCase();
  if (action === "ADD") {
    if (data.searches.length >= 8) throw new Error("Limit: 8 searches. Delete an old search first.");
    const fields = parseFields(words);
    const search = editSearch({
      id: "s" + data.next_id++, enabled: true, passengers: 1, mode: "snap", max: 60,
    }, fields, today);
    data.searches.push(search);
    return { data, changed: true, reply: "Added " + describe(search, today) + (data.enabled ? "" : "\nGlobal scanning is OFF. Send SCAN START.") };
  }
  const id = (words.shift() || "").toLowerCase();
  const search = data.searches.find((s) => s.id === id);
  if (!search) throw new Error("Unknown search ID. Send SEARCH LIST.");
  if (action === "SET") {
    if (!words.length) throw new Error("Supply fields to change, for example SEARCH SET " + id + " end=2026-10-12");
    editSearch(search, parseFields(words), today);
  } else if (["START", "STOP", "DELETE"].includes(action) && words.length === 0) {
    if (action === "START" && search.end_date < today) throw new Error("Search expired. Change its dates first.");
    if (action === "DELETE") data.searches = data.searches.filter((s) => s.id !== id);
    else search.enabled = action === "START";
  } else throw new Error("Use SEARCH ADD, SET, START, STOP, DELETE or LIST. Send HELP.");
  return { data, changed: true, reply: action === "DELETE" ? "Deleted " + id : describe(search, today) + (data.enabled ? "" : "\nGlobal scanning is OFF.") };
}

exports.handler = async function(context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  if (event.From !== context.OWNER_WHATSAPP || event.To !== context.BOT_WHATSAPP ||
      !/^SM[a-f0-9]{32}$/i.test(event.MessageSid || "")) return callback(null, twiml);
  const document = context.getTwilioClient().sync.v1.services(context.SYNC_SERVICE_SID).documents("controls");
  try {
    // Conditional writes preserve concurrent commands. Message IDs prevent retry replay.
    for (let attempt = 0; attempt < 3; attempt++) {
      const current = await document.fetch();
      if ((current.data.processed || []).includes(event.MessageSid)) return callback(null, twiml);
      let result;
      try { result = applyCommand(current.data, event.Body); }
      catch (error) {
        twiml.message(error.message);
        return callback(null, twiml);
      }
      result.data.processed = [...(current.data.processed || []), event.MessageSid].slice(-80);
      if (Buffer.byteLength(JSON.stringify(result.data), "utf8") > 15000) {
        twiml.message("Search storage is full. Delete an old search and retry.");
        return callback(null, twiml);
      }
      try {
        await document.update({ data: result.data, ifMatch: current.revision });
        // A reply must fit WhatsApp's free-form text limit.
        twiml.message(result.reply.slice(0, 1500));
        return callback(null, twiml);
      } catch (error) {
        if (error.status === 412) continue;
        throw error;
      }
    }
    throw new Error("concurrent changes");
  } catch (error) {
    console.error("Control request failed", error.status || "internal");
    twiml.message("Could not save or read your settings. Please retry shortly.");
    return callback(null, twiml);
  }
};
exports.applyCommand = applyCommand;
exports.todayIn = todayIn;
