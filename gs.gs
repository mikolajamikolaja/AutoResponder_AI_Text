/**
 * __AAA_processEmails - Google Apps Script
 * WERSJA 8 - ZUNIFIKOWANA LOGIKA WYSYŁKI:
 *
 * Dwie globalne flagi obliczane RAZ na początku pętli dla każdej osoby:
 *
 *   shouldSendZwykly = isAllowed || isKnownSender
 *     → osoba jest na ALLOWED_LIST LUB ma historię w HISTORY_SHEET_ID
 *     → zawsze dostaje odpowiedź zwykly.py
 *
 *   shouldSendSmierc = smircData !== null
 *     → osoba ma arkusz w SMIERC_HISTORY_SHEET_ID
 *     → zawsze dostaje odpowiedź smierc.py
 *
 * Obie flagi są niezależne — można dostać oba respondery równocześnie.
 * Flagi są używane konsekwentnie we WSZYSTKICH blokach (RE/FWD, JOKER,
 * SMIERC kontynuacja, nowa wiadomość).
 *
 * Pozostałe zmiany względem v7:
 *   - smircData obliczany RAZ na górze pętli (nie powtarzany w blokach)
 *   - wants_text_reply = shouldSendZwykly (nie zależy od bloku)
 *   - po każdej odpowiedzi backendu zawsze sprawdzane oba: zwykly i smierc
 *
 * Struktura SMIERC arkusza:
 * - Każda zakładka = email osoby (nazwa: email_z_underscore)
 * - Kolumna A: nr etapu | B: data_smierci | C: mail_od_osoby
 * - Kolumna D: odpowiedz_pawla | E: last_msg_id
 */

// ── Normalizacja tekstu ───────────────────────────────────────────────────────
function _normalize(text) {
  if (!text) return "";
  return text.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function _containsAny(haystack, keywords) {
  if (!haystack || !keywords || !keywords.length) return false;
  var normalizedHaystack = _normalize(haystack);
  return keywords.some(function(k) {
    if (!k) return false;
    return normalizedHaystack.indexOf(_normalize(k)) !== -1;
  });
}

function _getListFromProps(name) {
  var props = PropertiesService.getScriptProperties();
  var raw   = props.getProperty(name) || "";
  return raw.split(",").map(function(s){ return s.trim().toLowerCase(); }).filter(Boolean);
}

function escapeRegExp(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function removeKeywordsFromText(text, keywords, maskMode) {
  if (!text || !keywords || !keywords.length) return text;
  var sanitized = text;
  var sorted    = keywords.slice().filter(Boolean).sort(function(a, b){ return b.length - a.length; });
  sorted.forEach(function(k) {
    if (!k) return;
    var re = new RegExp(escapeRegExp(k), "gi");
    sanitized = sanitized.replace(re, maskMode ? "[REDACTED]" : "");
  });
  sanitized = sanitized.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n");
  return sanitized.trim();
}

function removeGeneratorPdfKeyword(text, keyword) {
  if (!text || !keyword) return text;
  var re = new RegExp(escapeRegExp(keyword), "gi");
  return text.replace(re, keyword.slice(0, -1));
}

function extractPlainTextFromHtml(htmlText) {
  if (!htmlText) return "";
  return htmlText
    .replace(/<!DOCTYPE[^>]*>/gi, "")
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, "")
    .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/\s+/g, " ").trim();
}

// ── Google Sheets: historia zwykłych wiadomości ───────────────────────────────
function saveToHistory(senderEmail, subject, body) {
  try {
    var sheetId = PropertiesService.getScriptProperties().getProperty("HISTORY_SHEET_ID");
    if (!sheetId) { console.warn("Brak HISTORY_SHEET_ID"); return; }
    var ss      = SpreadsheetApp.openById(sheetId);
    var sheet   = ss.getSheets()[0];
    var lastRow = sheet.getLastRow();
    var foundRow = -1;
    if (lastRow >= 2) {
      var emailCol = sheet.getRange(2, 2, lastRow - 1, 1).getValues();
      for (var i = 0; i < emailCol.length; i++) {
        if ((emailCol[i][0] || "").toString().toLowerCase().trim() === senderEmail.toLowerCase().trim()) {
          foundRow = i + 2; break;
        }
      }
    }
    var rowData = [new Date(), senderEmail, subject || "", (body || "").substring(0, 2000)];
    if (foundRow !== -1) {
      sheet.getRange(foundRow, 1, 1, 4).setValues([rowData]);
    } else {
      sheet.appendRow(rowData);
    }
    console.log("Historia zapisana dla: " + senderEmail);
  } catch (e) { console.error("Błąd zapisu historii: " + e.message); }
}

function findLastMessageBySender(senderEmail) {
  try {
    var sheetId = PropertiesService.getScriptProperties().getProperty("HISTORY_SHEET_ID");
    if (!sheetId) return null;
    var sheet   = SpreadsheetApp.openById(sheetId).getSheets()[0];
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) return null;
    var emailCol = sheet.getRange(2, 2, lastRow - 1, 1).getValues();
    for (var i = emailCol.length - 1; i >= 0; i--) {
      if ((emailCol[i][0] || "").toString().toLowerCase().trim() === senderEmail.toLowerCase().trim()) {
        var row = sheet.getRange(i + 2, 3, 1, 2).getValues()[0];
        return { subject: (row[0] || "").toString(), body: (row[1] || "").toString() };
      }
    }
    return null;
  } catch (e) { console.error("Błąd odczytu historii: " + e.message); return null; }
}

function _getKnownSenders() {
  try {
    var sheetId = PropertiesService.getScriptProperties().getProperty("HISTORY_SHEET_ID");
    if (!sheetId) return [];
    var sheet   = SpreadsheetApp.openById(sheetId).getSheets()[0];
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) return [];
    return sheet.getRange(2, 2, lastRow - 1, 1).getValues()
      .map(function(r){ return (r[0] || "").toString().toLowerCase().trim(); })
      .filter(Boolean);
  } catch(e) { console.error("Błąd _getKnownSenders: " + e.message); return []; }
}

function _isAlreadyProcessed(sender, subject) {
  try {
    var cache = CacheService.getScriptCache();
    var stored = cache.get("PROCESSED_IDS") || "[]";
    var list = JSON.parse(stored);
    if (!Array.isArray(list)) return false;
    for (var i = 0; i < list.length; i++) {
      var entry = list[i];
      if (entry.sender === sender && entry.subject === subject) {
        return true;
      }
    }
    return false;
  } catch (e) {
    console.error("Błąd sprawdzania PROCESSED_IDS: " + e.message);
    return false;
  }
}

function _recordProcessedStatus(sender, subject, processedStatus) {
  if (!sender || !subject || !processedStatus) return;
  var entry = {
    ts: new Date().toISOString(),
    sender: sender,
    subject: subject,
    processed_status: processedStatus
  };
  try {
    var cache = CacheService.getScriptCache();
    var stored = cache.get("PROCESSED_IDS") || "[]";
    var list = JSON.parse(stored);
    if (!Array.isArray(list)) list = [];
    list.push(entry);
    if (list.length > 100) list = list.slice(-100);
    cache.put("PROCESSED_IDS", JSON.stringify(list), 21600);
  } catch (e) {
    console.error("Błąd cache PROCESSED_IDS: " + e.message);
  }
  try {
    var props = PropertiesService.getScriptProperties();
    props.setProperty("PROCESSED_IDS", JSON.stringify(entry));
  } catch (e) {
    console.error("Błąd PropertiesService PROCESSED_IDS: " + e.message);
  }
}

function _reportBackendError(sender, subject, message) {
  try {
    var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
    if (!adminEmail) { console.warn("ADMIN_EMAIL nie ustawiony — nie wysyłam alertu"); return; }
    var body = "Render backend zgłosił problem dla wiadomości od: " + sender + "\n" +
               "Temat: " + subject + "\n" +
               "Szczegóły: " + message + "\n" +
               "Timestamp: " + new Date().toISOString();
    MailApp.sendEmail({
      to: adminEmail,
      subject: "[AUTORESPONDER] Błąd backendu rendera",
      body: body
    });
    console.log("Wysłano alert do ADMIN_EMAIL: " + adminEmail);
  } catch (e) {
    console.error("Błąd wysyłki alertu do ADMIN_EMAIL: " + e.message);
  }
}

function _reportRetryFailure(entry, message) {
  try {
    var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
    if (!adminEmail) { console.warn("ADMIN_EMAIL nie ustawiony — nie wysyłam retry alertu"); return; }
    var failed = entry.retry_responders || [];
    var details = entry.details ? JSON.stringify(entry.details) : "brak dodatkowych informacji";
    var body = "Retry responderów nie powiódł się dla wiadomości od: " + entry.sender + "\n" +
               "Temat: " + entry.subject + "\n" +
               "Respondery: " + failed.join(", ") + "\n" +
               "Próba: " + entry.attempt_count + "/2\n" +
               "Informacje: " + message + "\n" +
               "Szczegóły: " + details + "\n" +
               "Timestamp: " + new Date().toISOString();
    MailApp.sendEmail({
      to: adminEmail,
      subject: "[AUTORESPONDER] Retry responderów nieudany",
      body: body
    });
    console.log("Wysłano alert retry do ADMIN_EMAIL: " + adminEmail);
  } catch (e) {
    console.error("Błąd wysyłki retry alertu do ADMIN_EMAIL: " + e.message);
  }
}

function _loadPendingRetries() {
  try {
    var raw = PropertiesService.getScriptProperties().getProperty("PENDING_RETRY_QUEUE") || "[]";
    var list = JSON.parse(raw);
    return Array.isArray(list) ? list : [];
  } catch (e) {
    console.error("Błąd _loadPendingRetries: " + e.message);
    return [];
  }
}

function _savePendingRetries(list) {
  try {
    if (!Array.isArray(list)) { list = []; }
    if (list.length > 100) { list = list.slice(-100); }
    PropertiesService.getScriptProperties().setProperty("PENDING_RETRY_QUEUE", JSON.stringify(list));
  } catch (e) {
    console.error("Błąd _savePendingRetries: " + e.message);
  }
}

function _findPendingRetry(sender, subject) {
  var list = _loadPendingRetries();
  for (var i = 0; i < list.length; i++) {
    if (list[i].sender === sender && list[i].subject === subject) {
      return list[i];
    }
  }
  return null;
}

function _enqueuePendingRetry(payload, processedStatus) {
  if (!payload || !processedStatus || processedStatus.status !== "partial") return;
  if (processedStatus.attempt_count >= 2) return;

  var list = _loadPendingRetries();
  var existing = _findPendingRetry(payload.sender, payload.subject);
  var entry = existing || {
    id: "retry_" + Date.now() + "_" + Math.random().toString(36).slice(2),
    created_at: new Date().toISOString(),
    sender: payload.sender,
    sender_name: payload.sender_name || "",
    subject: payload.subject,
    thread_id: payload.thread_id || null,
    body: payload.body || "",
    previous_body: payload.previous_body || null,
    previous_subject: payload.previous_subject || null,
    isBiz: payload.isBiz || false,
    isAllowed: payload.isAllowed || false,
    isKnownSender: payload.isKnownSender || false,
    containsKeyword: payload.containsKeyword || false,
    containsKeywordTest: payload.test_mode || false,
    smircData: payload.smircData || null,
    retry_responders: [],
    attempt_count: 1,
    details: null,
    last_attempt_at: null,
  };

  entry.retry_responders = processedStatus.failed || entry.retry_responders;
  entry.attempt_count = processedStatus.attempt_count || entry.attempt_count;
  entry.details = processedStatus.details || entry.details;
  entry.last_attempt_at = new Date().toISOString();

  if (!existing) { list.push(entry); }
  _savePendingRetries(list);
}

function _getRetryAttachments(threadId) {
  if (!threadId) return [];
  try {
    var thread = GmailApp.getThreadById(threadId);
    if (!thread) return [];
    var messages = thread.getMessages();
    var msg = messages[messages.length - 1];
    return getAllAttachments(msg);
  } catch (e) {
    console.error("Błąd _getRetryAttachments: " + e.message);
    return [];
  }
}

function _processPendingRetries(webhookUrl) {
  var list = _loadPendingRetries();
  if (!list.length) return;
  console.log("Retry: przetwarzam " + list.length + " elementów");

  var updated = list.slice();
  for (var i = 0; i < list.length; i++) {
    var entry = list[i];
    if (entry.attempt_count >= 2) {
      _reportRetryFailure(entry, "Osiągnięto maksymalną liczbę prób (2). Najpierw usuń z kolejki, jeśli chcesz ponowić ręcznie.");
      updated = updated.filter(function(item) { return item.id !== entry.id; });
      continue;
    }

    var attachments = _getRetryAttachments(entry.thread_id);
    var response = _callBackend(
      entry.sender, entry.sender_name, entry.subject, entry.body, webhookUrl,
      entry.retry_responders.indexOf("scrabble") !== -1,
      entry.retry_responders.indexOf("analiza") !== -1,
      entry.retry_responders.indexOf("emocje") !== -1,
      entry.retry_responders.indexOf("obrazek") !== -1,
      entry.retry_responders.indexOf("generator_pdf") !== -1,
      entry.retry_responders.indexOf("smierc") !== -1,
      entry.smircData || null,
      attachments,
      entry.previous_body,
      entry.previous_subject,
      entry.isBiz,
      entry.isAllowed,
      entry.isKnownSender,
      entry.containsKeyword,
      entry.containsKeywordTest,
      entry.thread_id,
      entry.retry_responders,
      entry.attempt_count + 1
    );

    if (!response || !response.json) {
      entry.attempt_count += 1;
      _reportRetryFailure(entry, "Backend nieodpowiedział podczas próby retry.");
      if (entry.attempt_count >= 2) {
        updated = updated.filter(function(item) { return item.id !== entry.id; });
      }
      continue;
    }

    var status = response.json.processed_status || {};
    if (status.status === "ok") {
      updated = updated.filter(function(item) { return item.id !== entry.id; });
      continue;
    }

    entry.retry_responders = status.failed || entry.retry_responders;
    entry.details = status.details || entry.details;
    entry.attempt_count = status.attempt_count || entry.attempt_count + 1;
    entry.last_attempt_at = new Date().toISOString();
    _reportRetryFailure(entry, "Częściowy błąd podczas próby retry.");
    if (entry.attempt_count >= 2) {
      updated = updated.filter(function(item) { return item.id !== entry.id; });
    }
  }

  _savePendingRetries(updated);
}

// ── Moduł SMIERC ─────────────────────────────────────────────────────────────
function _getSmircData(senderEmail) {
  try {
    var sheetId = PropertiesService.getScriptProperties().getProperty("SMIERC_HISTORY_SHEET_ID");
    if (!sheetId) { console.warn("Brak SMIERC_HISTORY_SHEET_ID"); return null; }
    var ss        = SpreadsheetApp.openById(sheetId);
    var sheetName = senderEmail.replace("@", "_").replace(/\./g, "_");
    var sheet     = null;
    var sheets    = ss.getSheets();
    for (var i = 0; i < sheets.length; i++) {
      if (sheets[i].getName().toLowerCase().trim() === sheetName.toLowerCase().trim()) {
        sheet = sheets[i]; break;
      }
    }
    if (!sheet) { console.log("Brak arkusza dla: " + senderEmail); return null; }
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) { console.log("Arkusz pusty: " + senderEmail); return null; }
    var row     = sheet.getRange(lastRow, 1, 1, 5).getValues()[0];
    var etap    = parseInt(row[0]) || 1;
    var rawDate = sheet.getRange(2, 2).getValue();
    var data_smierci = "";
    if (!rawDate || rawDate.toString().trim() === "" || rawDate.toString().trim() === "nieznanego dnia") {
      data_smierci = _getDataSmierci();
      sheet.getRange(2, 2).setValue(data_smierci);
    } else if (rawDate instanceof Date) {
      data_smierci = Utilities.formatDate(rawDate, "GMT+1", "dd.MM.yyyy");
    } else {
      data_smierci = rawDate.toString().trim();
    }
    console.log("Smierc data dla " + senderEmail + ": etap=" + etap + " data=" + data_smierci);
    return { etap: etap, data_smierci: data_smierci, lastMsgId: row[4] ? row[4].toString() : "" };
  } catch (e) { console.error("Błąd _getSmircData: " + e.message); return null; }
}

function _updateSmircData(senderEmail, newEtap, bodyText, replyHtml, msgId) {
  try {
    var sheetId   = PropertiesService.getScriptProperties().getProperty("SMIERC_HISTORY_SHEET_ID");
    if (!sheetId) return;
    var ss        = SpreadsheetApp.openById(sheetId);
    var sheetName = senderEmail.replace("@", "_").replace(/\./g, "_");
    var sheet     = null;
    var sheets    = ss.getSheets();
    for (var i = 0; i < sheets.length; i++) {
      if (sheets[i].getName().toLowerCase().trim() === sheetName.toLowerCase().trim()) {
        sheet = sheets[i]; break;
      }
    }
    if (!sheet) { console.log("Brak arkusza dla: " + senderEmail); return; }
    var targetRow    = newEtap + 1;
    var safetyLimit  = 0;
    while (sheet.getLastRow() < targetRow && safetyLimit < 10) {
      safetyLimit++;
      sheet.appendRow(["", "", "", "", ""]);
    }
    var replyClean = extractPlainTextFromHtml(replyHtml || "");
    sheet.getRange(targetRow, 1, 1, 5).setValues([[
      newEtap, "", (bodyText || "").substring(0, 2000), replyClean, (msgId || "").toString().trim()
    ]]);
    if (targetRow !== 2) {
      var dataSmierci = sheet.getRange(2, 2).getValue().toString().trim();
      if (dataSmierci) sheet.getRange(targetRow, 2).setValue(dataSmierci);
    }
    console.log("Smierc zapisano: " + senderEmail + " etap=" + newEtap);
  } catch(e) { console.error("Błąd _updateSmircData: " + e.message); }
}

function _getDataSmierci() {
  var d = new Date();
  d.setDate(d.getDate() - 7);
  return String(d.getDate()).padStart(2, "0") + "." +
         String(d.getMonth() + 1).padStart(2, "0") + "." + d.getFullYear();
}

function _createSmircSheetForEmail(senderEmail, dataSmierciFallback) {
  try {
    var sheetId   = PropertiesService.getScriptProperties().getProperty("SMIERC_HISTORY_SHEET_ID");
    if (!sheetId) { console.warn("Brak SMIERC_HISTORY_SHEET_ID"); return false; }
    var ss        = SpreadsheetApp.openById(sheetId);
    var sheetName = senderEmail.replace("@", "_").replace(/\./g, "_");
    var sheets    = ss.getSheets();
    for (var i = 0; i < sheets.length; i++) {
      if (sheets[i].getName().toLowerCase().trim() === sheetName.toLowerCase().trim()) {
        _ensureSmircDate(sheets[i]); return true;
      }
    }
    var dataSmierci = (dataSmierciFallback && dataSmierciFallback !== "nieznanego dnia")
      ? dataSmierciFallback : _getDataSmierci();
    var newSheet = ss.insertSheet(sheetName);
    newSheet.getRange(1, 1, 1, 5).setValues([["nr_etapu", "data_smierci", "mail_od_osoby", "odpowiedz_pawla", "last_msg_id"]]);
    newSheet.getRange(2, 1, 1, 5).setValues([[1, dataSmierci, "", "", ""]]);
    console.log("Utworzono arkusz SMIERC dla: " + senderEmail + " data=" + dataSmierci);
    return true;
  } catch(e) { console.error("Błąd tworzenia arkusza SMIERC: " + e.message); return false; }
}

function _ensureSmircDate(sheet) {
  try {
    var b2 = sheet.getRange(2, 2).getValue().toString().trim();
    if (!b2 || b2 === "nieznanego dnia") {
      sheet.getRange(2, 2).setValue(_getDataSmierci());
    }
  } catch(e) { console.error("Błąd _ensureSmircDate: " + e.message); }
}

function _isNewMessage(subject) {
  var s = (subject || "").toLowerCase().trim();
  return !(s.startsWith("re:") || s.startsWith("fwd:") ||
           s.startsWith("fw:") || s.startsWith("odp:") || s.startsWith("aw:"));
}

function getAllAttachments(msg) {
  var attachments = msg.getAttachments();
  if (!attachments || !attachments.length) return [];
  var docTypes = [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword", "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.text-template",
    "application/pdf", "text/plain",
  ];
  var result = [];
  for (var i = 0; i < attachments.length; i++) {
    var att  = attachments[i];
    var mime = att.getContentType();
    var name = att.getName().toLowerCase();
    var isDoc = docTypes.indexOf(mime) !== -1 ||
                name.endsWith(".docx") || name.endsWith(".doc") ||
                name.endsWith(".odt")  || name.endsWith(".ott") ||
                name.endsWith(".pdf")  || name.endsWith(".txt");
    if (isDoc) result.push({ base64: Utilities.base64Encode(att.getBytes()), name: att.getName() });
  }
  return result;
}

// ════════════════════════════════════════════════════════════════════════════
// GOOGLE DRIVE — zapis wszystkich plików
// ════════════════════════════════════════════════════════════════════════════

function _getDriveFolder() {
  var folderId = PropertiesService.getScriptProperties().getProperty("DRIVE_FOLDER_ID");
  if (!folderId) { console.error("[Drive] Brak DRIVE_FOLDER_ID"); return null; }
  var rootFolder = DriveApp.getFolderById(folderId);
  var today      = Utilities.formatDate(new Date(), "GMT+1", "yyyy-MM-dd");
  var it         = rootFolder.getFoldersByName(today);
  return it.hasNext() ? it.next() : rootFolder.createFolder(today);
}

function _saveFileToDriveNow(fileObj) {
  if (!fileObj || !fileObj.base64) return;
  try {
    var folder   = _getDriveFolder();
    if (!folder) return;
    var filename = fileObj.filename || "plik.bin";
    var existing = folder.getFilesByName(filename);
    if (existing.hasNext()) { console.log("[Drive] Duplikat — pomijam: " + filename); return; }
    folder.createFile(Utilities.newBlob(
      Utilities.base64Decode(fileObj.base64),
      fileObj.content_type || "application/octet-stream",
      filename
    ));
    console.log("[Drive] Zapisano sync: " + filename);
  } catch(e) { console.error("[Drive] Błąd sync zapisu: " + e.message); }
}

function _queueDriveFile(key, fileObj) {
  if (!fileObj || !fileObj.base64) return;
  var serialized = JSON.stringify({
    base64:       fileObj.base64,
    content_type: fileObj.content_type || "application/octet-stream",
    filename:     fileObj.filename     || key + ".bin"
  });
  if (serialized.length > 90000) {
    console.warn("[DriveQ] Duży plik (" + (fileObj.filename || key) + ") — zapisuję sync");
    _saveFileToDriveNow(fileObj);
    return;
  }
  try {
    var cache    = CacheService.getScriptCache();
    var existing = cache.get("driveq_keys") || "";
    var keys     = existing ? existing.split(",") : [];
    var cacheKey = "driveq_" + key + "_" + Date.now();
    cache.put(cacheKey, serialized, 21600);
    keys.push(cacheKey);
    cache.put("driveq_keys", keys.join(","), 21600);
    console.log("[DriveQ] Kolejka: " + (fileObj.filename || key));
  } catch(e) {
    console.error("[DriveQ] Błąd kolejkowania: " + e.message);
    _saveFileToDriveNow(fileObj);
  }
}

function _queueAllFromSection(sectionData) {
  if (!sectionData || typeof sectionData !== "object") return;
  var SINGLE_FIELDS = [
    "pdf", "emoticon", "cv_pdf", "log_psych",
    "ankieta_html", "ankieta_pdf", "horoskop_pdf",
    "karta_rpg_pdf", "raport_pdf", "debug_txt",
    "explanation_txt", "plakat_svg", "gra_html",
    "image", "image2", "prompt1_txt", "prompt2_txt",
  ];
  var LIST_FIELDS = [
    "triptych", "images", "videos", "docs", "docx_list",
  ];
  SINGLE_FIELDS.forEach(function(field) {
    _queueDriveFile(field, sectionData[field]);
  });
  LIST_FIELDS.forEach(function(field) {
    var arr = sectionData[field];
    if (!Array.isArray(arr)) return;
    arr.forEach(function(item, idx) {
      _queueDriveFile(field + "_" + idx, item);
    });
  });
}

function _scheduleDriveFlush() {
  try {
    ScriptApp.getProjectTriggers().forEach(function(t) {
      if (t.getHandlerFunction() === "__AAA_driveFlush") ScriptApp.deleteTrigger(t);
    });
    ScriptApp.newTrigger("__AAA_driveFlush").timeBased().after(2 * 60 * 1000).create();
    console.log("[DriveQ] Trigger driveFlush zaplanowany za 2 min");
  } catch(e) { console.error("[DriveQ] Błąd tworzenia triggera: " + e.message); }
}

function __AAA_driveFlush() {
  var cache    = CacheService.getScriptCache();
  var existing = cache.get("driveq_keys");
  if (!existing) { console.log("[DriveFlush] Kolejka pusta"); return; }
  var folder = _getDriveFolder();
  if (!folder) return;
  var keys   = existing.split(",").filter(Boolean);
  var saved  = 0;
  var failed = 0;
  keys.forEach(function(cacheKey) {
    try {
      var raw = cache.get(cacheKey);
      if (!raw) { failed++; return; }
      var obj = JSON.parse(raw);
      if (!obj.base64) { failed++; return; }
      var filename  = obj.filename || "plik.bin";
      var dupCheck = folder.getFilesByName(filename);
      if (dupCheck.hasNext()) {
        console.log("[DriveFlush] Duplikat — pomijam: " + filename);
        cache.remove(cacheKey);
        return;
      }
      folder.createFile(Utilities.newBlob(
        Utilities.base64Decode(obj.base64),
        obj.content_type || "application/octet-stream",
        filename
      ));
      console.log("[DriveFlush] Zapisano: " + filename);
      cache.remove(cacheKey);
      saved++;
    } catch(e) {
      console.error("[DriveFlush] Błąd " + cacheKey + ": " + e.message);
      failed++;
    }
  });
  cache.remove("driveq_keys");
  console.log("[DriveFlush] DONE: zapisano=" + saved + " błędów=" + failed);
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === "__AAA_driveFlush") ScriptApp.deleteTrigger(t);
  });
}

// ════════════════════════════════════════════════════════════════════════════
// WYSYŁKA
// ════════════════════════════════════════════════════════════════════════════

function executeMailSend(data, recipient, subject, msg, senderName) {
  var inlineImages = {};
  var attachments  = [];

  if (data.emoticon && data.emoticon.base64) {
    try {
      inlineImages["emotka_cid"] = Utilities.newBlob(
        Utilities.base64Decode(data.emoticon.base64),
        data.emoticon.content_type || "image/png",
        data.emoticon.filename     || "emotka.png"
      );
    } catch (e) { console.error("[zwykly] Błąd emotki: " + e.message); }
  }
  if (data.cv_pdf && data.cv_pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.cv_pdf.base64), "application/pdf",
        data.cv_pdf.filename || "CV_Tyler.pdf"
      ));
    } catch (e) { console.error("[zwykly] Błąd CV PDF: " + e.message); }
  }
  if (data.log_psych && data.log_psych.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.log_psych.base64),
        data.log_psych.content_type || "text/plain",
        data.log_psych.filename     || "log_psych.txt"
      ));
    } catch (e) { console.error("[zwykly] Błąd log_psych: " + e.message); }
  }
  if (data.ankieta_html && data.ankieta_html.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.ankieta_html.base64), "text/html",
        data.ankieta_html.filename || "ankieta.html"
      ));
    } catch (e) { console.error("[zwykly] Błąd ankieta HTML: " + e.message); }
  }
  if (data.ankieta_pdf && data.ankieta_pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.ankieta_pdf.base64), "application/pdf",
        data.ankieta_pdf.filename || "ankieta.pdf"
      ));
    } catch (e) { console.error("[zwykly] Błąd ankieta PDF: " + e.message); }
  }
  if (data.horoskop_pdf && data.horoskop_pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.horoskop_pdf.base64), "application/pdf",
        data.horoskop_pdf.filename || "horoskop.pdf"
      ));
    } catch (e) { console.error("[zwykly] Błąd horoskop PDF: " + e.message); }
  }
  if (data.karta_rpg_pdf && data.karta_rpg_pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.karta_rpg_pdf.base64), "application/pdf",
        data.karta_rpg_pdf.filename || "karta_rpg.pdf"
      ));
    } catch (e) { console.error("[zwykly] Błąd karta RPG: " + e.message); }
  }
  if (data.raport_pdf && data.raport_pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.raport_pdf.base64),
        data.raport_pdf.content_type || "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data.raport_pdf.filename     || "raport_psychiatryczny.docx"
      ));
    } catch (e) { console.error("[zwykly] Błąd raport: " + e.message); }
  }

  var triptychHtml   = "";
  var triptychImages = data.triptych || [];
  if (triptychImages.length > 0) {
    triptychHtml += '<hr style="border:none;border-top:2px solid #C8B89A;margin:28px 0 16px;">';
    triptychHtml += '<p style="font-size:11px;color:#888;margin:0 0 10px;text-align:center;letter-spacing:1px;">TYLER DURDEN — TRYPTYK</p>';
    triptychHtml += '<table cellpadding="0" cellspacing="6" style="margin:0 auto;"><tr>';
    triptychImages.forEach(function(imgObj, index) {
      if (!imgObj || !imgObj.base64) return;
      try {
        var cid      = "tyler_panel_" + index;
        var filename = imgObj.filename || ("tyler_panel_" + index + ".jpg");
        var mime     = imgObj.content_type || "image/jpeg";
        inlineImages[cid] = Utilities.newBlob(Utilities.base64Decode(imgObj.base64), mime, filename);
        attachments.push(Utilities.newBlob(Utilities.base64Decode(imgObj.base64), mime, filename));
        triptychHtml += '<td valign="top" style="text-align:center;padding:2px;">' +
          '<img src="cid:' + cid + '" alt="Panel ' + (index + 1) + '"' +
          ' style="max-width:185px;border-radius:4px;border:1px solid #C8B89A;"></td>';
      } catch (e) { console.error("[zwykly] Błąd panelu " + index + ": " + e.message); }
    });
    triptychHtml += '</tr></table>';
  }

  if (data.explanation_txt && data.explanation_txt.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.explanation_txt.base64), "text/plain",
        data.explanation_txt.filename || "wyjasnienie.txt"
      ));
    } catch (e) { console.error("[zwykly] Błąd wyjaśnienia TXT: " + e.message); }
  }
  if (data.plakat_svg && data.plakat_svg.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.plakat_svg.base64), "image/svg+xml",
        data.plakat_svg.filename || "plakat.svg"
      ));
    } catch (e) { console.error("[zwykly] Błąd plakat SVG: " + e.message); }
  }
  if (data.gra_html && data.gra_html.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.gra_html.base64), "text/html",
        data.gra_html.filename || "gra.html"
      ));
    } catch (e) { console.error("[zwykly] Błąd gra HTML: " + e.message); }
  }

  var htmlBody = (data.reply_html || "<p>(Brak treści)</p>") + triptychHtml;
  try {
    msg.reply("", { htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: senderName });
    console.log("[zwykly] Wysłano: " + senderName + " → " + recipient + " | att=" + attachments.length);
  } catch (e) {
    try {
      MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: senderName });
      console.log("[zwykly] sendEmail() fallback OK → " + recipient);
    } catch (e2) { console.error("[zwykly] sendEmail() zawiódł: " + e2.message); }
  }
}

function executeGeneratorPdfMailSend(data, recipient, subject, msg) {
  if (!data) { console.warn("Brak danych generator_pdf dla " + recipient); return; }
  var attachments = [];
  if (data.pdf && data.pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.pdf.base64), "application/pdf",
        data.pdf.filename || "egzamin.pdf"
      ));
    } catch (e) { console.error("Błąd PDF egzaminu: " + e.message); }
  }
  var htmlBody = data.reply_html || "<p>Oto PDF aktywny.</p>";
  try {
    msg.reply("", { htmlBody: htmlBody, attachments: attachments, name: "Generator Egzaminów – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [egzamin PDF]", htmlBody: htmlBody, attachments: attachments, name: "Generator Egzaminów – Autoresponder" });
  }
}

function executeSmircMailSend(data, recipient, subject, msg, newEtap) {
  if (!data) { console.warn("Brak danych smierc dla " + recipient); return; }
  var attachments  = [];
  var inlineImages = {};
  var imagesHtml   = "";

  if (data.images && Array.isArray(data.images)) {
    data.images.forEach(function(imgObj, index) {
      try {
        if (imgObj.base64) {
          var cid     = "smirc_img_" + index;
          var imgBlob = Utilities.newBlob(
            Utilities.base64Decode(imgObj.base64),
            imgObj.content_type || "image/png",
            imgObj.filename     || ("obraz_" + index + ".png")
          );
          inlineImages[cid] = imgBlob;
          attachments.push(imgBlob);
          imagesHtml += '<p><img src="cid:' + cid + '" alt="Zaswiety" style="max-width:100%;border-radius:8px;margin-bottom:10px;"></p>';
        }
      } catch(e) { console.error("Blad obrazka " + index + ": " + e.message); }
    });
  }
  if (data.videos && Array.isArray(data.videos)) {
    data.videos.forEach(function(vidObj, index) {
      try {
        if (vidObj.base64) {
          attachments.push(Utilities.newBlob(
            Utilities.base64Decode(vidObj.base64),
            vidObj.content_type || "video/mp4",
            vidObj.filename     || ("niebo_" + index + ".mp4")
          ));
        }
      } catch(e) { console.error("Blad wideo " + index + ": " + e.message); }
    });
  }
  if (data.debug_txt && data.debug_txt.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.debug_txt.base64),
        data.debug_txt.content_type || "text/plain",
        data.debug_txt.filename     || "_.txt"
      ));
    } catch(e) { console.error("Blad debug TXT: " + e.message); }
  }

  var htmlBody = "<div>" + (data.reply_html || "<p>(Brak tresci)</p>") + imagesHtml + "</div>";
  try {
    msg.reply("", { htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Autoresponder zza swiatowy" });
    console.log("Wyslano smierc (etap " + newEtap + ") do " + recipient + ". Zalacznikow: " + attachments.length);
  } catch(e) {
    try {
      MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Autoresponder zza swiatowy" });
    } catch(e2) { console.error("sendEmail() tez fail: " + e2.message); }
  }
}

function executeScrabbleMailSend(data, recipient, subject, msg) {
  var inlineImages = {};
  var attachments  = [];
  if (data.image && data.image.base64) {
    try {
      var imgBlob = Utilities.newBlob(
        Utilities.base64Decode(data.image.base64),
        data.image.content_type || "image/png",
        data.image.filename     || "scrabble_odpowiedz.png"
      );
      inlineImages["scrabble_cid"] = imgBlob;
      attachments.push(imgBlob);
    } catch (e) { console.error("Błąd obrazka scrabble: " + e.message); }
  }
  var htmlBody = (data.reply_html || "") +
    (inlineImages["scrabble_cid"] ? '<p><img src="cid:scrabble_cid" alt="Scrabble" style="max-width:100%;"></p>' : "");
  try {
    msg.reply("", { htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Scrabble – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Scrabble – Autoresponder" });
  }
}

function executeAnalizaMailSend(data, recipient, subject, msg) {
  if (!data) { console.warn("Brak danych analizy dla " + recipient); return; }
  var attachments = [];
  var docxList    = data.docx_list || [];
  for (var i = 0; i < docxList.length; i++) {
    var d = docxList[i];
    if (!d || !d.base64) continue;
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(d.base64),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        d.filename || ("analiza_" + (i + 1) + ".docx")
      ));
    } catch (e) { console.error("Błąd DOCX [" + i + "]: " + e.message); }
  }
  var htmlBody = data.reply_html || "<p>Analiza powtórzeń w załączniku.</p>";
  try {
    msg.reply("", { htmlBody: htmlBody, attachments: attachments, name: "Analiza Powtórzeń – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, attachments: attachments, name: "Analiza Powtórzeń – Autoresponder" });
  }
}

function executeEmocjeMailSend(data, recipient, subject, msg) {
  if (!data) { console.warn("Brak danych emocji dla " + recipient); return; }
  var attachments = [];
  (data.images || []).forEach(function(img, i) {
    if (!img || !img.base64) return;
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(img.base64), img.content_type || "image/png",
        img.filename || ("wykres_" + (i + 1) + ".png")
      ));
    } catch (e) { console.error("Błąd wykresu [" + i + "]: " + e.message); }
  });
  (data.docs || []).forEach(function(doc, j) {
    if (!doc || !doc.base64) return;
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(doc.base64), doc.content_type || "text/plain",
        doc.filename || ("raport_" + (j + 1) + ".txt")
      ));
    } catch (e) { console.error("Błąd raportu TXT [" + j + "]: " + e.message); }
  });
  var htmlBody = data.reply_html || "<p>Analiza emocjonalna w załącznikach.</p>";
  try {
    msg.reply("", { htmlBody: htmlBody, attachments: attachments, name: "Analiza Emocjonalna – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, attachments: attachments, name: "Analiza Emocjonalna – Autoresponder" });
  }
}

function _callWebhookGif(png1Base64, png2Base64, webhookUrl) {
  var gifUrl  = webhookUrl.replace("/webhook", "/webhook_gif");
  var options = {
    method: "post", contentType: "application/json",
    payload: JSON.stringify({ png1_base64: png1Base64 || null, png2_base64: png2Base64 || null }),
    muteHttpExceptions: true
  };
  try {
    var resp = UrlFetchApp.fetch(gifUrl, options);
    if (resp.getResponseCode() === 200) {
      return JSON.parse(resp.getContentText());
    }
    console.error("webhook_gif błąd: " + resp.getResponseCode());
    return null;
  } catch(e) { console.error("webhook_gif wyjątek: " + e.message); return null; }
}

function executeObrazekMailSend(data, recipient, subject, msg) {
  if (!data) { console.warn("Brak danych obrazka dla " + recipient); return; }
  var webhookUrl = PropertiesService.getScriptProperties().getProperty("WEBHOOK_URL");

  if (data.image && data.image.base64) {
    try {
      var inlineImages1 = {};
      var attachments1  = [];
      var imgBlob1 = Utilities.newBlob(
        Utilities.base64Decode(data.image.base64),
        data.image.content_type || "image/png",
        data.image.filename     || "komiks_ai.png"
      );
      inlineImages1["obrazek_cid"] = imgBlob1;
      attachments1.push(imgBlob1);
      if (data.prompt1_txt && data.prompt1_txt.base64) {
        attachments1.push(Utilities.newBlob(
          Utilities.base64Decode(data.prompt1_txt.base64),
          data.prompt1_txt.content_type || "text/plain",
          data.prompt1_txt.filename     || "2222.txt"
        ));
      }
      var htmlBody1 = (data.reply_html || "<p>Obrazek AI.</p>") +
        '<p><img src="cid:obrazek_cid" alt="Komiks AI" style="max-width:100%;border-radius:8px;"></p>';
      try {
        msg.reply("", { htmlBody: htmlBody1, inlineImages: inlineImages1, attachments: attachments1, name: "Komiks AI – Autoresponder" });
      } catch(e) {
        MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [komiks]", htmlBody: htmlBody1, inlineImages: inlineImages1, attachments: attachments1, name: "Komiks AI – Autoresponder" });
      }
    } catch(e) { console.error("Błąd obrazka 1: " + e.message); }
  }

  if (data.image2 && data.image2.base64) {
    try {
      var inlineImages2 = {};
      var attachments2  = [];
      var imgBlob2 = Utilities.newBlob(
        Utilities.base64Decode(data.image2.base64),
        data.image2.content_type || "image/png",
        data.image2.filename     || "komiks_ai_retro.png"
      );
      inlineImages2["obrazek2_cid"] = imgBlob2;
      attachments2.push(imgBlob2);
      if (data.prompt2_txt && data.prompt2_txt.base64) {
        attachments2.push(Utilities.newBlob(
          Utilities.base64Decode(data.prompt2_txt.base64),
          data.prompt2_txt.content_type || "text/plain",
          data.prompt2_txt.filename     || "3333.txt"
        ));
      }
      var htmlBody2 = "<p>Ta sama historia, styl retro-pop lata 60.:</p>" +
        '<p><img src="cid:obrazek2_cid" alt="Komiks Retro" style="max-width:100%;border-radius:8px;"></p>';
      try {
        msg.reply("", { htmlBody: htmlBody2, inlineImages: inlineImages2, attachments: attachments2, name: "Komiks Retro – Autoresponder" });
      } catch(e) {
        MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [retro]", htmlBody: htmlBody2, inlineImages: inlineImages2, attachments: attachments2, name: "Komiks Retro – Autoresponder" });
      }
    } catch(e) { console.error("Błąd obrazka 2: " + e.message); }
  }

  var png1b64 = (data.image  && data.image.base64)  ? data.image.base64  : null;
  var png2b64 = (data.image2 && data.image2.base64) ? data.image2.base64 : null;
  if ((png1b64 || png2b64) && webhookUrl) {
    var gifData = _callWebhookGif(png1b64, png2b64, webhookUrl);
    if (gifData && gifData.gif1 && gifData.gif1.base64) {
      try {
        var gifBlob1     = Utilities.newBlob(Utilities.base64Decode(gifData.gif1.base64), gifData.gif1.content_type || "image/gif", gifData.gif1.filename || "komiks_ai.gif");
        var htmlBodyGif1 = "<p>Komiks AI – animacja:</p><p><i>Animowany GIF w załączniku.</i></p>";
        try {
          msg.reply("", { htmlBody: htmlBodyGif1, attachments: [gifBlob1], name: "Komiks AI GIF – Autoresponder" });
        } catch(e) {
          MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [komiks GIF]", htmlBody: htmlBodyGif1, attachments: [gifBlob1], name: "Komiks AI GIF – Autoresponder" });
        }
      } catch(e) { console.error("Błąd GIF 1: " + e.message); }
    }
    if (gifData && gifData.gif2 && gifData.gif2.base64) {
      try {
        var gifBlob2     = Utilities.newBlob(Utilities.base64Decode(gifData.gif2.base64), gifData.gif2.content_type || "image/gif", gifData.gif2.filename || "komiks_ai_retro.gif");
        var htmlBodyGif2 = "<p>Komiks Retro – animacja:</p><p><i>Animowany GIF w załączniku.</i></p>";
        try {
          msg.reply("", { htmlBody: htmlBodyGif2, attachments: [gifBlob2], name: "Komiks Retro GIF – Autoresponder" });
        } catch(e) {
          MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [retro GIF]", htmlBody: htmlBodyGif2, attachments: [gifBlob2], name: "Komiks Retro GIF – Autoresponder" });
        }
      } catch(e) { console.error("Błąd GIF 2: " + e.message); }
    }
  }
}

function executeNawiazanieMailSend(data, recipient, subject, msg) {
  if (!data || !data.has_history || !data.reply_html) return;
  try {
    msg.reply("", { htmlBody: data.reply_html, name: "Nawiązanie – Autoresponder" });
    console.log("Wysłano nawiązanie -> " + recipient);
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [nawiązanie]", htmlBody: data.reply_html, name: "Nawiązanie – Autoresponder" });
  }
}

// ── Wywołanie backendu ────────────────────────────────────────────────────────
function _callBackend(sender, senderName, subject, body, url,
                      wantsScrabble, wantsAnaliza, wantsEmocje, wantsObrazek,
                      wantsGeneratorPdf, wantsSmierc, smircData,
                      attachments, previousBody, previousSubject,
                      isBiz, isAllowed, isKnownSender, containsKeyword,
                      testMode, threadId, retryResponders, attemptCount) {
  var secret  = PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET");
  var payload = {
    sender:              sender,
    sender_name:         senderName        || "",
    subject:             subject,
    body:                body,
    wants_scrabble:      wantsScrabble      ? true : false,
    wants_analiza:       wantsAnaliza       ? true : false,
    wants_emocje:        wantsEmocje        ? true : false,
    wants_obrazek:       wantsObrazek       ? true : false,
    wants_generator_pdf: wantsGeneratorPdf  ? true : false,
    wants_smierc:        wantsSmierc        ? true : false,
    etap:                smircData ? smircData.etap         : 1,
    data_smierci:        smircData ? smircData.data_smierci : "nieznanego dnia",
    historia:            smircData ? smircData.historia     : [],
    wants_text_reply:    (isBiz || isAllowed || isKnownSender || containsKeyword) ? true : false,
    attachments:         attachments        || [],
    previous_body:       previousBody       || null,
    previous_subject:    previousSubject    || null,
    save_to_drive:       false,
    test_mode:           testMode          ? true : false,
    thread_id:           threadId         || null,
    retry_responders:    retryResponders  || [],
    attempt_count:       attemptCount     || 1
  };

  console.log("Webhook — sender: " + payload.sender +
              " | smierc=" + payload.wants_smierc +
              " | etap=" + payload.etap + " | data=" + payload.data_smierci +
              " | text_reply=" + payload.wants_text_reply);

  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            JSON.stringify(payload),
    muteHttpExceptions: true,
    headers:            secret ? { "X-Webhook-Secret": secret } : {}
  };
  try {
    var resp = UrlFetchApp.fetch(url, options);
    if (resp.getResponseCode() === 200) {
      var text = resp.getContentText();
      try {
        var json = JSON.parse(text);
        console.log("Webhook — odpowiedź OK (200)");
        if (json && json.processed_status) {
          _recordProcessedStatus(payload.sender, payload.subject, json.processed_status);
        }
        return { json: json };
      } catch (e) {
        console.error("Błąd parsowania JSON z backendu: " + e.message);
        _reportBackendError(payload.sender, payload.subject, "Błąd parsowania JSON: " + e.message + " | odpowiedź: " + text);
        return null;
      }
    }
    var errMsg = "Backend zwrócił kod " + resp.getResponseCode();
    console.error(errMsg);
    _reportBackendError(payload.sender, payload.subject, errMsg + " | body: " + resp.getContentText());
  } catch (e) {
    var errMsg = "Błąd połączenia z backendem: " + e.message;
    console.error(errMsg);
    _reportBackendError(payload.sender, payload.subject, errMsg);
  }
  return null;
}

function _stripQuotedText(body) {
  if (!body) return "";
  var lines  = body.split("\n");
  var result = [];
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (line.startsWith(">") ||
        line.match(/^(wt|śr|czw|pt|sob|niedz|pon)\.,?\s+\d/i) ||
        line.match(/^(On|W dniu|Am)\s+.+wrote:/i) ||
        line.match(/^Dnia\s+\d/i) ||
        line.match(/napisał\(a\):/i)) break;
    result.push(lines[i]);
  }
  return result.join("\n").trim();
}

function __AAA_processEmails() {
  var props      = PropertiesService.getScriptProperties();
  var webhookUrl = props.getProperty("WEBHOOK_URL");
  if (!webhookUrl) { console.error("Brak WEBHOOK_URL!"); return; }

  // Najpierw przetwórz oczekujące retry
  _processPendingRetries(webhookUrl);

  var BIZ_LIST                   = _getListFromProps("BIZ_LIST");
  var ALLOWED_LIST               = _getListFromProps("ALLOWED_LIST");
  var ALLOWED_LIST_GENERATOR_PDF = _getListFromProps("ALLOWED_LIST_GENERATOR_PDF");
  var KEYWORDS                   = _getListFromProps("KEYWORDS");
  var KEYWORDS1                  = _getListFromProps("KEYWORDS1");
  var KEYWORDS2                  = _getListFromProps("KEYWORDS2");
  var KEYWORDS3                  = _getListFromProps("KEYWORDS3");
  var KEYWORDS4                  = _getListFromProps("KEYWORDS4");
  var KEYWORDS_JOKER             = _getListFromProps("KEYWORDS_JOKER");
  var KEYWORDS_OBRAZEK           = _getListFromProps("KEYWORDS_OBRAZEK");
  var KEYWORDS_GENERATOR_PDF     = _getListFromProps("KEYWORDS_GENERATOR_PDF");
  var KEYWORDS_SMIERC            = _getListFromProps("KEYWORDS_SMIERC");
  var KEYWORDS_TEST              = _getListFromProps("KEYWORDS_TEST");
  var DATA_SMIERCI               = props.getProperty("DATA_SMIERCI") || "nieznanego dnia";

  var maskMode      = false;
  var knownSenders  = _getKnownSenders();

  console.log("Znani nadawcy: " + knownSenders.length);

  // Pobierz wątki i odwróć kolejność — zaczynamy od najstarszego
  var threads = GmailApp.getInboxThreads(0, 80).reverse();
  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    if (!thread.isUnread()) continue;

    var webhookCalled = false;  // reset dla każdego wątku — każdy mail może wywołać backend
    var messages   = thread.getMessages();
    var msg        = messages[messages.length - 1];
    var fromRaw    = msg.getFrom();
    var fromEmail  = extractEmail(fromRaw).toLowerCase();
    var senderName = "";
    var nameMatch  = fromRaw.match(/^"?([^"<]+)"?\s*</);
    if (nameMatch) senderName = nameMatch[1].trim();
    var subject    = msg.getSubject();
    var plainBody  = _stripQuotedText(msg.getPlainBody());
    var searchText = plainBody + " " + subject;

    // Sprawdź czy wiadomość już była przetworzona
    if (_isAlreadyProcessed(fromEmail, subject)) {
      console.log("Wiadomość już przetworzona, pomijam: " + fromEmail + " | " + subject);
      thread.markRead();
      continue;
    }

    // ── FLAGI GLOBALNE — obliczane RAZ dla każdej osoby ──────────────────────
    var isBiz         = BIZ_LIST.indexOf(fromEmail) !== -1;
    var isAllowed     = ALLOWED_LIST.indexOf(fromEmail) !== -1;
    var isKnownSender = knownSenders.indexOf(fromEmail) !== -1;

    // smircData obliczany raz — używany we wszystkich blokach
    var smircData      = _getSmircData(fromEmail);
    var isSmierc       = smircData !== null;

    // Dwie główne flagi decydujące o wysyłce:
    // shouldSendZwykly: znam tę osobę (allowed LUB ma historię)
    // shouldSendSmierc: osoba ma arkusz śmierci
    var shouldSendZwykly = isAllowed || isKnownSender;
    var shouldSendSmierc = isSmierc;

    var isNewMsg = _isNewMessage(subject);
    console.log("DEBUG: isNewMsg=" + isNewMsg + " from=" + fromEmail +
                " | zwykly=" + shouldSendZwykly + " | smierc=" + shouldSendSmierc);

    // ── ODPOWIEDŹ (RE:/FWD:) ─────────────────────────────────────────────────
    if (!isNewMsg) {
      console.log("Odpowiedź (RE:/FWD:) od: " + fromEmail);

      if (shouldSendSmierc || shouldSendZwykly) {
        thread.markRead();
        var previousDataReply = findLastMessageBySender(fromEmail);
        if (webhookCalled) { thread.markRead(); continue; }
        webhookCalled = true;

        var responseReply = _callBackend(
          fromEmail, senderName, subject, plainBody, webhookUrl,
          false, false, false, false, false,
          shouldSendSmierc, smircData, [],
          previousDataReply ? previousDataReply.body    : null,
          previousDataReply ? previousDataReply.subject : null,
          isBiz, isAllowed, isKnownSender, false,
          containsKeywordTest
        );

        if (responseReply && responseReply.json) {
          if (shouldSendSmierc && responseReply.json.smierc) {
            var newEtapReply = responseReply.json.smierc.nowy_etap || smircData.etap;
            executeSmircMailSend(responseReply.json.smierc, fromEmail, subject, msg, newEtapReply);
            _updateSmircData(fromEmail, newEtapReply, plainBody, responseReply.json.smierc.reply_html, msg.getId());
          }
          if (shouldSendZwykly && responseReply.json.zwykly) {
            executeMailSend(responseReply.json.zwykly, fromEmail, subject, msg, "Tyler Durden – Autoresponder");
          }
        }
        saveToHistory(fromEmail, subject, plainBody);
        break; // obsłużono — przerywamy, następny mail przy kolejnym triggerze

      } else {
        var labelRe = GmailApp.getUserLabelByName("processed") || GmailApp.createLabel("processed");
        thread.addLabel(labelRe);
        thread.markRead();
      }
      continue; // RE/FWD bez flagi — tylko oznacz i idź dalej
    }

    // ── NOWA WIADOMOŚĆ ────────────────────────────────────────────────────────
    var isAllowedGeneratorPdf       = ALLOWED_LIST_GENERATOR_PDF.indexOf(fromEmail) !== -1;
    var containsKeywordGeneratorPdf = _containsAny(searchText, KEYWORDS_GENERATOR_PDF);
    var wantsGeneratorPdf           = isAllowedGeneratorPdf || containsKeywordGeneratorPdf;

    // ── JOKER ────────────────────────────────────────────────────────────────
    var containsJoker = _containsAny(searchText, KEYWORDS_JOKER);
    if (containsJoker) {
      console.log("🃏 JOKER! Aktywacja dla: " + fromEmail);

      // Jeśli nie ma jeszcze arkusza śmierci — utwórz i ustaw flagę
      if (!isSmierc) {
        if (_createSmircSheetForEmail(fromEmail, DATA_SMIERCI)) {
          smircData      = _getSmircData(fromEmail) || { etap: 1, data_smierci: DATA_SMIERCI, historia: [] };
          isSmierc       = true;
          shouldSendSmierc = true;
        }
      }
      // JOKER zawsze wysyła wszystko — w tym zwykly jeśli znamy osobę
      // (po JOKER osoba trafia do historii, więc przy kolejnym mailu shouldSendZwykly=true)
      thread.markRead();
      if (webhookCalled) { thread.markRead(); continue; }
      webhookCalled = true;

      var responseJoker = _callBackend(
        fromEmail, senderName, subject, plainBody, webhookUrl,
        true, true, true, true, true,
        shouldSendSmierc, smircData, getAllAttachments(msg),
        findLastMessageBySender(fromEmail) ? findLastMessageBySender(fromEmail).body    : null,
        findLastMessageBySender(fromEmail) ? findLastMessageBySender(fromEmail).subject : null,
        isBiz, isAllowed, isKnownSender, true,
        containsKeywordTest
      );

      if (responseJoker && responseJoker.json) {
        var jj = responseJoker.json;
        if (jj.biznes)        executeMailSend(jj.biznes, fromEmail, subject, msg, "Notariusz – Informacja");
        if (jj.zwykly)        executeMailSend(jj.zwykly, fromEmail, subject, msg, "Tyler Durden – Autoresponder");
        if (jj.scrabble)      executeScrabbleMailSend(jj.scrabble, fromEmail, subject, msg);
        if (jj.analiza)       executeAnalizaMailSend(jj.analiza, fromEmail, subject, msg);
        if (jj.emocje)        executeEmocjeMailSend(jj.emocje, fromEmail, subject, msg);
        if (jj.obrazek)       executeObrazekMailSend(jj.obrazek, fromEmail, subject, msg);
        if (jj.nawiazanie)    executeNawiazanieMailSend(jj.nawiazanie, fromEmail, subject, msg);
        if (jj.generator_pdf) executeGeneratorPdfMailSend(jj.generator_pdf, fromEmail, subject, msg);
        if (shouldSendSmierc && jj.smierc) {
          var newEtapJoker = jj.smierc.nowy_etap || smircData.etap;
          executeSmircMailSend(jj.smierc, fromEmail, subject, msg, newEtapJoker);
          _updateSmircData(fromEmail, newEtapJoker, plainBody, jj.smierc.reply_html, msg.getId());
        }
        console.log("🃏 JOKER: wszystkie respondery obsłużone");
      }
      saveToHistory(fromEmail, subject, plainBody);
      break; // JOKER obsłużony — przerywamy
    }

    // ── SMIERC kontynuacja (nowa wiadomość od osoby z arkuszem śmierci) ──────
    if (shouldSendSmierc) {
      console.log("SMIERC kontynuacja: " + fromEmail + " etap=" + smircData.etap);
      thread.markRead();

      var previousData2 = findLastMessageBySender(fromEmail);
      if (webhookCalled) { thread.markRead(); continue; }
      webhookCalled = true;

      var response2 = _callBackend(
        fromEmail, senderName, subject, plainBody, webhookUrl,
        false, false, false, false, false,
        true, smircData, [],
        previousData2 ? previousData2.body    : null,
        previousData2 ? previousData2.subject : null,
        isBiz, isAllowed, isKnownSender, false,
        containsKeywordTest
      );

      if (response2 && response2.json) {
        if (response2.json.smierc) {
          var newEtap2 = response2.json.smierc.nowy_etap || smircData.etap;
          executeSmircMailSend(response2.json.smierc, fromEmail, subject, msg, newEtap2);
          _updateSmircData(fromEmail, newEtap2, plainBody, response2.json.smierc.reply_html, msg.getId());
        }
        // shouldSendZwykly — niezależna flaga, działa równocześnie ze śmiercią
        if (shouldSendZwykly && response2.json.zwykly) {
          executeMailSend(response2.json.zwykly, fromEmail, subject, msg, "Tyler Durden – Autoresponder");
        }
      }
      saveToHistory(fromEmail, subject, plainBody);
      break; // SMIERC kontynuacja obsłużona — przerywamy
    }

    // ── SMIERC start (nowe słowo kluczowe SMIERC, brak arkusza) ──────────────
    var containsKeywordSmierc = _containsAny(searchText, KEYWORDS_SMIERC);
    if (containsKeywordSmierc) {
      if (_createSmircSheetForEmail(fromEmail, DATA_SMIERCI)) {
        smircData        = _getSmircData(fromEmail);
        isSmierc         = true;
        shouldSendSmierc = true;
        console.log("SMIERC start: " + fromEmail);
      }
    }

    var containsKeyword        = _containsAny(searchText, KEYWORDS)  || _containsAny(searchText, KEYWORDS1);
    var containsKeyword2       = _containsAny(searchText, KEYWORDS2);
    var containsKeyword3       = _containsAny(searchText, KEYWORDS3);
    var containsKeyword4       = _containsAny(searchText, KEYWORDS4);
    var containsKeywordObrazek = _containsAny(searchText, KEYWORDS_OBRAZEK);
    var containsKeywordTest    = _containsAny(searchText, KEYWORDS_TEST);

    // Aktualizuj shouldSendZwykly jeśli zawiera słowo kluczowe
    if (containsKeyword) shouldSendZwykly = true;

    if (!isBiz && !shouldSendZwykly && !containsKeyword2 && !containsKeyword3 &&
        !containsKeyword4 && !containsKeywordObrazek && !wantsGeneratorPdf && !shouldSendSmierc) {
      var labelSkip = GmailApp.getUserLabelByName("processed") || GmailApp.createLabel("processed");
      thread.addLabel(labelSkip);
      thread.markRead();
      continue;
    }

    var combinedKeywords = KEYWORDS.concat(KEYWORDS1).concat(KEYWORDS2).concat(KEYWORDS3)
      .concat(KEYWORDS4).concat(KEYWORDS_JOKER).concat(KEYWORDS_OBRAZEK).concat(KEYWORDS_SMIERC).filter(Boolean);
    var sanitizedBody = removeKeywordsFromText(plainBody, combinedKeywords, maskMode);

    if (containsKeywordGeneratorPdf) {
      KEYWORDS_GENERATOR_PDF.forEach(function(k) {
        sanitizedBody = removeGeneratorPdfKeyword(sanitizedBody, k);
        subject       = removeGeneratorPdfKeyword(subject, k);
      });
    }

    var previousData    = findLastMessageBySender(fromEmail);
    var previousBody    = previousData ? previousData.body    : null;
    var previousSubject = previousData ? previousData.subject : null;

    var allAttachments = [];
    if (containsKeyword3 || containsKeyword4) {
      allAttachments = getAllAttachments(msg);
      console.log("Załączników: " + allAttachments.length);
    }

    if (!smircData && !shouldSendSmierc) {
      smircData = { etap: 1, data_smierci: DATA_SMIERCI, historia: [] };
    }

    thread.markRead();
    if (webhookCalled) { continue; }
    webhookCalled = true;

    var response = _callBackend(
      fromEmail, senderName, subject, sanitizedBody, webhookUrl,
      containsKeyword2, containsKeyword3, containsKeyword4, containsKeywordObrazek,
      wantsGeneratorPdf, shouldSendSmierc, smircData, allAttachments,
      previousBody, previousSubject,
      isBiz, isAllowed, isKnownSender, containsKeyword,
      containsKeywordTest
    );

    if (response && response.json) {
      var json = response.json;

      if (json.biznes && (isBiz || (!isAllowed && containsKeyword))) {
        executeMailSend(json.biznes, fromEmail, subject, msg, "Notariusz – Informacja");
      }
      if (json.zwykly && shouldSendZwykly) {
        executeMailSend(json.zwykly, fromEmail, subject, msg, "Tyler Durden – Autoresponder");
      }
      if (containsKeyword2 && json.scrabble) {
        executeScrabbleMailSend(json.scrabble, fromEmail, subject, msg);
      }
      if (containsKeyword3 && json.analiza) {
        executeAnalizaMailSend(json.analiza, fromEmail, subject, msg);
      }
      if (containsKeyword4 && json.emocje) {
        executeEmocjeMailSend(json.emocje, fromEmail, subject, msg);
      }
      if (containsKeywordObrazek && json.obrazek) {
        executeObrazekMailSend(json.obrazek, fromEmail, subject, msg);
      }
      if (json.nawiazanie) {
        executeNawiazanieMailSend(json.nawiazanie, fromEmail, subject, msg);
      }
      if (wantsGeneratorPdf && json.generator_pdf) {
        executeGeneratorPdfMailSend(json.generator_pdf, fromEmail, subject, msg);
      }
      if (shouldSendSmierc && json.smierc) {
        var newEtap = json.smierc.nowy_etap || smircData.etap;
        executeSmircMailSend(json.smierc, fromEmail, subject, msg, newEtap);
        _updateSmircData(fromEmail, newEtap, sanitizedBody, json.smierc.reply_html, msg.getId());
      }
      saveToHistory(fromEmail, subject, sanitizedBody);
      break; // główny blok obsłużony — przerywamy, następny przy kolejnym triggerze
    } else {
      console.warn("Backend nie odpowiedział dla: " + fromEmail);
    }
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function extractEmail(fromHeader) {
  var m = fromHeader.match(/<([^>]+)>/);
  if (m) return m[1];
  return fromHeader.split(" ")[0];
}

function keepAlive() {
  var url = PropertiesService.getScriptProperties().getProperty("WEBHOOK_URL");
  try { UrlFetchApp.fetch(url.replace("/webhook", "/"), { muteHttpExceptions: true }); } catch(e) {}
}
