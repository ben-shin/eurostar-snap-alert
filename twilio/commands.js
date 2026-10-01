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


const MENU = "Hi! I can watch Eurostar fares for you.\n1 Add a search\n2 My searches\n3 Pause all alerts\n4 Resume all alerts\n5 Help\nReply with a number, or send MENU anytime.";
const CITY_KEYS = Object.keys(STATIONS), FIELDS = ["from","to","start","end","passengers","mode","max"];
const SESSION_MS = 30 * 60 * 1000;
function choose(text, options) {
  const input = text.trim().toLowerCase(), n = Number(input);
  return /^[1-9]$/.test(input) && n <= options.length ? n - 1 :
    options.findIndex((option) => option.toLowerCase() === input);
}
function answer(field, text, draft) {
  const value = text.trim();
  if (field === "from" || field === "to") {
    const index = choose(value, CITY_KEYS);
    if (index < 0) throw Error("Choose a city by number or name.");
    if (draft[field === "from" ? "to" : "from"] === CITY_KEYS[index]) throw Error("Choose two different cities.");
    return CITY_KEYS[index];
  }
  if (field === "start" || field === "end") {
    const match = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(value);
    const date = match ? [match[3],match[2].padStart(2,"0"),match[1].padStart(2,"0")].join("-") : value;
    if (!validDate(date)) throw Error("Send a real date, like 05/10/2026 or 2026-10-05.");
    return date;
  }
  if (field === "passengers") {
    if (!/^[1-4]$/.test(value)) throw Error("Reply 1, 2, 3 or 4.");
    return Number(value);
  }
  if (field === "mode") {
    const index = choose(value, ["snap","normal","both"]);
    if (index < 0) throw Error("Reply 1 for Snap, 2 for normal, or 3 for both.");
    return ["snap","normal","both"][index];
  }
  if (!/^\d+(\.\d{1,2})?$/.test(value) || Number(value) <= 0 || Number(value) > 500)
    throw Error("Send an amount above 0 and at most 500, like 45.50.");
  return Number(value);
}
function steps(draft) {
  return ["from","to","start","end","passengers","mode",...(draft.mode && draft.mode !== "snap" ? ["max"] : [])];
}
function summary(draft) {
  return (draft.from ? STATIONS[draft.from] : draft.origin) + " -> " +
    (draft.to ? STATIONS[draft.to] : draft.destination) + "\n" +
    (draft.start || draft.start_date) + " to " + (draft.end || draft.end_date) +
    "; " + draft.passengers + " passenger(s)\n" + draft.mode + " fares" +
    (draft.mode === "snap" ? "" : "; max " + draft.max + " GBP/EUR");
}
function prompt(session, data, today) {
  if (session.stage === "add" || session.stage === "value") {
    const field = session.stage === "add" ? session.step : session.field;
    const labels = {
      from:"Where will you leave from?",to:"Where are you going?",
      start:"First travel date? Send DD/MM/YYYY.",end:"Last travel date? Send DD/MM/YYYY.",
      passengers:"How many passengers? Reply 1, 2, 3 or 4.",
      mode:"Which fares? 1 Snap, 2 normal, 3 both.",
      max:"Highest normal fare to alert you about? Send 1 to 500 (GBP/EUR)."
    };
    return labels[field] + (["from","to"].includes(field) ?
      "\n1 London  2 Brussels  3 Paris\n4 Amsterdam  5 Rotterdam  6 Lille" : "") +
      "\nSend BACK or CANCEL anytime.";
  }
  if (session.stage === "select") return data.searches.length ?
    "Which search? Reply with its number:\n" + data.searches.map((s,i) =>
      (i+1) + " " + s.origin + " -> " + s.destination + " (" + status(s,today) + ")").join("\n") +
      "\nSend BACK for the menu." : "You have no searches yet. Send MENU to add one.";
  const search = data.searches.find((s) => s.id === session.search_id);
  if (session.stage === "action") return search.origin + " -> " + search.destination +
    "\n1 Edit\n2 " + (search.enabled ? "Pause" : "Resume") +
    "\n3 Delete\n4 Details\nReply with a number, BACK or CANCEL.";
  if (session.stage === "field") return "What would you like to change?\n1 From  2 To\n3 First date  4 Last date\n5 Passengers  6 Fare type\n7 Max normal fare\nReply with a number or BACK.";
  if (session.stage === "confirm_add") return "Add this search?\n" + summary(session.draft) + "\nReply YES or NO. Send BACK to edit.";
  if (session.stage === "confirm_edit") return "Save this change?\n" + summary({...search,...session.draft}) +
    "\nReply YES or NO. Send BACK to choose a field.";
  if (session.stage === "confirm_delete") return "Delete " + search.origin + " -> " + search.destination +
    "? This cannot be undone. Reply YES or NO, or BACK.";
  return MENU;
}
function saved(data, owner, session, reply) {
  data.conversations = session ? {[owner]:session} : {};
  return {data,changed:true,reply};
}
function applyInput(original, body, owner, now = new Date()) {
  const data = JSON.parse(JSON.stringify(original));
  if (data.version !== 1 || !Array.isArray(data.searches)) throw Error("Invalid control document.");
  const input = String(body || "").trim(), lower = input.toLowerCase(), today = todayIn(data.timezone,now);
  let session = data.conversations?.[owner];
  if (!session || session.expires_at <= now.getTime()) session = null;
  if (/^(scan|search|status|test)(\s|$)/i.test(input)) {
    const result = applyCommand(data,input,now);
    return saved(result.data,owner,null,result.reply);
  }
  if (["hi","hello","hey","menu","help","cancel"].includes(lower))
    return saved(data,owner,null,(lower === "cancel" ? "Cancelled.\n" : "") + MENU);
  if (lower === "commands") return saved(data,owner,null,HELP);
  if (lower === "back" && session) {
    if (session.stage === "add") {
      const i = steps(session.draft).indexOf(session.step);
      if (i <= 0) return saved(data,owner,null,MENU);
      session.step = steps(session.draft)[i-1];
    } else if (session.stage === "confirm_add") {
      session.stage = "add"; session.step = steps(session.draft).at(-1);
    } else if (session.stage === "select") return saved(data,owner,null,MENU);
    else if (session.stage === "action") session.stage = "select";
    else if (session.stage === "field") session.stage = "action";
    else if (["value","confirm_edit"].includes(session.stage)) session.stage = "field";
    else if (session.stage === "confirm_delete") session.stage = "action";
    session.expires_at = now.getTime()+SESSION_MS;
    return saved(data,owner,session,prompt(session,data,today));
  }
  if (!session) {
    const aliases = {add:"add a search",searches:"my searches",pause:"pause all alerts",resume:"resume all alerts"};
    const option = choose(aliases[lower] || lower,["add a search","my searches","pause all alerts","resume all alerts","help"]);
    if (option === 0) {
      if (data.searches.length >= 8) return saved(data,owner,null,"You have 8 searches already. Delete one first.\n"+MENU);
      session = {stage:"add",step:"from",draft:{passengers:1,mode:"snap",max:60}};
    } else if (option === 1) session = {stage:"select"};
    else if (option === 2 || option === 3) {
      data.enabled = option === 3;
      return saved(data,owner,null,data.enabled ?
        "Alerts resumed. Active searches run on the next scheduled scan.\nSend MENU for more choices." :
        "All fare alerts paused. An in-flight check may finish.\nSend MENU for more choices.");
    } else if (option === 4) return saved(data,owner,null,MENU+"\nSend COMMANDS for older text commands. Scans run about every 15 minutes.");
    else return saved(data,owner,null,(lower === "back" ? "That conversation has ended.\n" : "I didn't catch that.\n")+MENU);
    session.expires_at = now.getTime()+SESSION_MS;
    return saved(data,owner,session,prompt(session,data,today));
  }
  session.expires_at = now.getTime()+SESSION_MS;
  if (session.stage === "add" || session.stage === "value") {
    const field = session.stage === "add" ? session.step : session.field;
    try { session.draft[field] = answer(field,input,session.draft); }
    catch(error) { return saved(data,owner,session,error.message+"\n"+prompt(session,data,today)); }
    if (session.stage === "value") session.stage = "confirm_edit";
    else {
      const ordered = steps(session.draft), i = ordered.indexOf(field);
      if (i === ordered.length-1) session.stage = "confirm_add";
      else session.step = ordered[i+1];
    }
    return saved(data,owner,session,prompt(session,data,today));
  }
  if (session.stage === "select") {
    const index = choose(input,data.searches.map((s)=>s.id));
    if (index < 0) return saved(data,owner,session,"Please reply with a search number.\n"+prompt(session,data,today));
    session.stage = "action"; session.search_id = data.searches[index].id;
    return saved(data,owner,session,prompt(session,data,today));
  }
  const search = data.searches.find((s)=>s.id === session.search_id);
  if (!search && session.stage !== "confirm_add") return saved(data,owner,null,"That search is no longer here.\n"+MENU);
  if (session.stage === "action") {
    const option = choose(input,["edit",search.enabled ? "pause" : "resume","delete","details"]);
    if (option === 0) { session.stage="field"; session.draft={}; }
    else if (option === 1) {
      if (!search.enabled && search.end_date < today) return saved(data,owner,session,
        "This search expired. Edit its dates before resuming.\n"+prompt(session,data,today));
      search.enabled = !search.enabled;
      return saved(data,owner,null,"Search "+(search.enabled ? "resumed: " : "paused: ")+
        search.origin+" -> "+search.destination+".\nSend MENU for more choices.");
    } else if (option === 2) session.stage="confirm_delete";
    else if (option === 3) return saved(data,owner,session,summary(search)+"\n"+prompt(session,data,today));
    else return saved(data,owner,session,"Please choose 1, 2, 3 or 4.\n"+prompt(session,data,today));
    return saved(data,owner,session,prompt(session,data,today));
  }
  if (session.stage === "field") {
    const index = choose(input,FIELDS);
    if (index < 0) return saved(data,owner,session,"Please choose a field from 1 to 7.\n"+prompt(session,data,today));
    session.field=FIELDS[index]; session.stage="value";
    return saved(data,owner,session,prompt(session,data,today));
  }
  if (["confirm_add","confirm_edit","confirm_delete"].includes(session.stage)) {
    if (["no","n"].includes(lower)) return saved(data,owner,null,"No changes made.\n"+MENU);
    if (!["yes","y"].includes(lower)) return saved(data,owner,session,"Please reply YES or NO.\n"+prompt(session,data,today));
    if (session.stage === "confirm_delete") {
      data.searches=data.searches.filter((s)=>s.id !== session.search_id);
      return saved(data,owner,null,"Deleted "+search.origin+" -> "+search.destination+".\n"+MENU);
    }
    try {
      if (session.stage === "confirm_add") {
        if (data.searches.length >= 8) throw Error("You have 8 searches already. Delete one first.");
        const item=editSearch({id:"s"+data.next_id,enabled:true,passengers:1,mode:"snap",max:60},session.draft,today);
        data.next_id++; data.searches.push(item);
        return saved(data,owner,null,"Added your search. "+
          (data.enabled ? "It will run on the next scheduled scan." : "Alerts are paused; resume from MENU.")+"\n"+MENU);
      }
      const updated=editSearch({...search},session.draft,today);
      data.searches=data.searches.map((s)=>s.id === updated.id ? updated : s);
      return saved(data,owner,null,"Saved your search.\n"+MENU);
    } catch(error) {
      session.stage=session.stage === "confirm_add" ? "add" : "field";
      if (session.stage === "add") session.step=session.draft.end ? "end" : "start";
      return saved(data,owner,session,error.message+"\n"+prompt(session,data,today));
    }
  }
  return saved(data,owner,null,MENU);
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
      try { result = applyInput(current.data, event.Body, event.From); }
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
exports.applyInput = applyInput;
exports.todayIn = todayIn;
