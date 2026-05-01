/**
 * __AAA_processEmails - Google Apps Script
 * WERSJA 13 - ASYNC PIPELINE, BEZ RETRY:
 *
 * Architektura:
 *   GAS: odbierz email → nadaj etykietę processed → zapisz ODEBRANO do Sheets → POST do Render → koniec
 *   Render: wykonaj pipeline → wyślij mail → zapisz WYSŁANO do Sheets
 *
 * Deduplikacja opiera się wyłącznie na etykiecie Gmail "autoresponder-processed".
 * Brak retry, brak kolejek,.
 */

// ── Normalizacja tekstu ───────────────────────────────────────────────────────
function _normalize(text) {
  if (!text) return "";
  return text.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function _wordRegex(word) {
  if (!word) return null;
  var normalizedWord = _normalize(word);
  if (!normalizedWord) return null;
  var escaped = escapeRegExp(normalizedWord);
  return new RegExp('(^|[^\\w])' + escaped + '([^\\w]|$)', "i");
}

function _containsAny(haystack, keywords) {
  if (!haystack || !keywords || !keywords.length) return false;
  var normalizedHaystack = _normalize(haystack);
  return keywords.some(function(k) {
    if (!k) return false;
    var re = _wordRegex(k);
    return re ? re.test(normalizedHaystack) : false;
  });
}

function _findMatchingKeywords(haystack, keywords) {
  if (!haystack || !keywords || !keywords.length) return [];
  var normalizedHaystack = _normalize(haystack);
  return keywords
    .filter(Boolean)
    .filter(function(k) {
      var re = _wordRegex(k);
      return re ? re.test(normalizedHaystack) : false;
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
    var re = new RegExp('\\b' + escapeRegExp(k) + '\\b', "gi");
    sanitized = sanitized.replace(re, maskMode ? "[REDACTED]" : "");
  });
  sanitized = sanitized.replace(/[ \t]{2,}/g, " ").replace(/\n{3,}/g, "\n\n");
  return sanitized.trim();
}

function removeGeneratorPdfKeyword(text, keyword) {
  if (!text || !keyword) return text;
  var re = new RegExp('\\b' + escapeRegExp(keyword) + '\\b', "gi");
  return text.replace(re, keyword.slice(0, -1));
}

function extractPlainTextFromHtml(htmlText) {
  if (!htmlText) return "";
  return htmlText
    .replace(/<!DOCTYPE[^>]*>/gi, "")
    .replace(/<script\b[\s\S]*?<\/script>/gi, "")
    .replace(/<style\b[\s\S]*?<\/style>/gi, "")
    .replace(/<head\b[\s\S]*?<\/head>/gi, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/\s+/g, " ").trim();
}

// ── Google Sheets: historia zwykłych wiadomości ───────────────────────────────
function _getHistorySheet() {
  var sheetId = PropertiesService.getScriptProperties().getProperty("HISTORY_SHEET_ID");
  if (!sheetId) { console.warn("Brak HISTORY_SHEET_ID"); return null; }

  var ss = SpreadsheetApp.openById(sheetId);
  var sheet = ss.getSheetByName("Historia");
  if (!sheet) {
    console.warn("Brak zakładki 'Historia' w arkuszu historii, używam pierwszej zakładki.");
    sheet = ss.getSheets()[0];
  }
  return sheet;
}

/**
 * Zapisuje wiersz ODEBRANO do arkusza historii PRZED wysłaniem do backendu.
 * Nowa struktura kolumn:
 *   message_id | sender | data | temat | status_gas | status_render | responder | treść
 */
function _saveOdebranoToHistory(messageId, senderEmail, subject, body) {
  try {
    var sheet = _getHistorySheet();
    if (!sheet) return;
    var ts = new Date().toISOString();
    var cleanBody = (body || "").substring(0, 1000);
    var rowData = [messageId, senderEmail, ts, subject || "", "ODEBRANO", "", "", cleanBody];
    sheet.appendRow(rowData);
    console.log("ODEBRANO zapisano dla: " + senderEmail + " msg_id=" + messageId);
  } catch (e) { console.error("Błąd _saveOdebranoToHistory: " + e.message); }
}

function saveToHistory(senderEmail, subject, body) {
  try {
    var sheet = _getHistorySheet();
    if (!sheet) return;
    var rowData = [new Date(), senderEmail, "WEJŚCIE", subject || "", (body || "").substring(0, 1000)];
    sheet.appendRow(rowData);
    console.log("Historia zapisana dla: " + senderEmail);
  } catch (e) { console.error("Błąd zapisu historii: " + e.message); }
}

function saveResponseToHistory(senderEmail, subject, body) {
  try {
    var sheet = _getHistorySheet();
    if (!sheet) return;
    var plainText = extractPlainTextFromHtml(body || "");
    var rowData = [new Date(), senderEmail, "ODPOWIEDŹ", subject || "", plainText.substring(0, 1000)];
    sheet.appendRow(rowData);
    console.log("Odpowiedź zapisana dla: " + senderEmail);
  } catch (e) { console.error("Błąd zapisu odpowiedzi: " + e.message); }
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
        var typeCell = sheet.getRange(i + 2, 3, 1, 1).getValues()[0][0];
        if (typeCell === "WEJŚCIE") {
          var row = sheet.getRange(i + 2, 4, 1, 2).getValues()[0];
          return { subject: (row[0] || "").toString(), body: (row[1] || "").toString() };
        }
      }
    }
    return null;
  } catch (e) { console.error("Błąd odczytu historii: " + e.message); return null; }
}

function _getKnownSenders() {
  try {
    var sheetId = PropertiesService.getScriptProperties().getProperty("HISTORY_SHEET_ID");
    if (!sheetId) return [];
    var ss      = SpreadsheetApp.openById(sheetId);
    var sheet   = ss.getSheetByName("Historia") || ss.getSheets()[0];
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) return [];
    return sheet.getRange(2, 2, lastRow - 1, 1).getValues()
      .map(function(r){ return (r[0] || "").toString().toLowerCase().trim(); })
      .filter(Boolean);
  } catch(e) { console.error("Błąd _getKnownSenders: " + e.message); return []; }
}





function _reportBackendError(sender, subject, message) {
  try {
    var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
    if (!adminEmail) { console.warn("ADMIN_EMAIL nie ustawiony — nie wysyłam alertu"); return; }
    var body = [
      "Render backend zgłosił problem dla wiadomości od: " + sender,
      "Temat: " + subject,
      "Szczegóły: " + message,
      "Timestamp: " + new Date().toISOString()
    ].join("\r\n");
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
// GOOGLE DRIVE — zapis plików (nadal dostępny jako helper)
// ════════════════════════════════════════════════════════════════════════════

function _getDriveFolder() {
  var folderId = PropertiesService.getScriptProperties().getProperty("DRIVE_FOLDER_ID");
  if (!folderId) { console.error("[Drive] Brak DRIVE_FOLDER_ID"); return null; }
  var rootFolder = DriveApp.getFolderById(folderId);
  var today      = Utilities.formatDate(new Date(), "GMT+1", "yyyy-MM-dd");
  var it         = rootFolder.getFoldersByName(today);
  return it.hasNext() ? it.next() : rootFolder.createFolder(today);
}

function _appendPrzeplywSheetRow(row) {
  if (!row.wysylka) {
    console.log("[Flow] Pominieto — wysylka === false");
    return;
  }

  try {
    var sheetId = PropertiesService.getScriptProperties().getProperty("DECYZJA_WYSYLKI_SHEET_ID");
    if (!sheetId) { console.warn("[Flow] Brak DECYZJA_WYSYLKI_SHEET_ID"); return; }
    var sheet = SpreadsheetApp.openById(sheetId).getSheets()[0];

    var headers = [
      "ts", "from", "subject", "isNewMsg", "KEYWORDS", "KEYWORDS1", "KEYWORDS2", "KEYWORDS3",
      "KEYWORDS4", "KEYWORDS_GENERATOR_PDF", "KEYWORDS_SMIERC", "JOKER",
      "lista_smiert", "lista_historia", "flaga_test", "wysylka", "action", "notes"
    ];

    var lastRow = sheet.getLastRow();
    var needsHeaders = true;

    if (lastRow > 0) {
      var firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
      if (firstRow[0] === "ts" && firstRow[15] === "wysylka") {
        needsHeaders = false;
      }
    }

    if (needsHeaders) {
      sheet.clearContents();
      sheet.appendRow(headers);
      console.log("[Flow] Dodano nagłówki do arkusza");
    }

    var values = [
      row.ts || Utilities.formatDate(new Date(), "GMT+1", "yyyy-MM-dd HH:mm:ss"),
      row.fromEmail,
      (row.subject || "").substring(0, 120),
      row.isNewMsg ? "tak" : "nie",
      row.KEYWORDS ? "tak" : "nie",
      row.KEYWORDS1 ? "tak" : "nie",
      row.KEYWORDS2 ? "tak" : "nie",
      row.KEYWORDS3 ? "tak" : "nie",
      row.KEYWORDS4 ? "tak" : "nie",
      row.KEYWORDS_GENERATOR_PDF ? "tak" : "nie",
      row.KEYWORDS_SMIERC ? "tak" : "nie",
      row.JOKER ? "tak" : "nie",
      row.lista_smiert ? "tak" : "nie",
      row.lista_historia ? "tak" : "nie",
      row.flaga_test ? "tak" : "nie",
      row.wysylka ? "tak" : "nie",
      row.action || "",
      row.notes || ""
    ];

    sheet.appendRow(values);
    var newRowNum = sheet.getLastRow();

    for (var i = 0; i < values.length; i++) {
      if (values[i] === "tak") {
        sheet.getRange(newRowNum, i + 1).setBackground("#90EE90");
      }
    }

    console.log("[Flow] Dodano wiersz #" + newRowNum + " (wysylka OK)");
  } catch (e) {
    console.error("[Flow] Błąd zapisu do arkusza: " + e.message);
  }
}

// ════════════════════════════════════════════════════════════════════════════
// WYSYŁKA — funkcje pomocnicze (używane tylko przez stare ścieżki / fallback)
// UWAGA: W nowej architekturze Render sam wysyła maile. Poniższe funkcje
// zachowane dla kompatybilności (np. executeSmircMailSend wymagana lokalnie
// jeśli chcemy fallback GAS-side dla śmierci).
// ════════════════════════════════════════════════════════════════════════════

function executeMailSend(data, recipient, subject, msg, senderName) {
  if (!data) { console.warn("Brak danych dla " + recipient); return; }
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) {
    console.log("[zwykly] BLOKADA: Nie wysyłam do ADMIN_EMAIL (" + recipient + ")");
    return;
  }
  var inlineImages = {};
  var attachments  = [];
  var attachedNames = {};

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

  var htmlBody = (data.reply_html || "<p>(Brak treści)</p>") + triptychHtml;
  try {
    msg.reply("", { htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: senderName });
    console.log("[zwykly] Wysłano: " + senderName + " → " + recipient);
  } catch (e) {
    try {
      MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: senderName });
    } catch (e2) { console.error("[zwykly] sendEmail() zawiódł: " + e2.message); }
  }
}

function addBase64Attachment(attachments, attachedNames, d, defaultName, defaultContentType) {
  if (!d || !d.base64) return;
  var filename = d.filename || defaultName;
  if (attachedNames[filename]) return;
  attachedNames[filename] = true;
  try {
    attachments.push(Utilities.newBlob(
      Utilities.base64Decode(d.base64),
      d.content_type || defaultContentType,
      filename
    ));
  } catch (e) {
    console.error("Błąd attachment " + filename + ": " + e.message);
  }
}

function attachLogFiles(data, attachments, attachedNames) {
  addBase64Attachment(attachments, attachedNames, data.log_txt, "log.txt", "text/plain");
  addBase64Attachment(attachments, attachedNames, data.log_svg, "log.svg", "image/svg+xml");
}

function sectionWithLogs(sectionData, rootJson) {
  if (!sectionData || !rootJson) return sectionData;
  if (!sectionData.log_txt && rootJson.log_txt) sectionData.log_txt = rootJson.log_txt;
  if (!sectionData.log_svg && rootJson.log_svg) sectionData.log_svg = rootJson.log_svg;
  return sectionData;
}

function executeGeneratorPdfMailSend(data, recipient, subject, msg) {
  if (!data) { console.warn("Brak danych generator_pdf dla " + recipient); return; }
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) { return; }
  var attachments = [];
  var attachedNames = {};
  if (data.pdf && data.pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.pdf.base64), "application/pdf",
        data.pdf.filename || "dokument.pdf"
      ));
    } catch (e) { console.error("[gen_pdf] Błąd PDF: " + e.message); }
  }
  attachLogFiles(data, attachments, attachedNames);
  var htmlBody = data.reply_html || "<p>Dokument w załączniku.</p>";
  try {
    msg.reply("", { htmlBody: htmlBody, attachments: attachments, name: "Generator PDF – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, attachments: attachments });
  }
}

function executeSmircMailSend(data, recipient, subject, msg, newEtap) {
  if (!data) { console.warn("Brak danych smierc dla " + recipient); return; }
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) { return; }
  var inlineImages = {};
  var attachments  = [];
  var attachedNames = {};
  var imagesHtml = "";

  if (data.images && Array.isArray(data.images)) {
    data.images.forEach(function(imgObj, index) {
      try {
        if (imgObj && imgObj.base64) {
          var cid = "smierc_" + index;
          var imgBlob = Utilities.newBlob(
            Utilities.base64Decode(imgObj.base64),
            imgObj.content_type || "image/png",
            imgObj.filename     || ("obraz_" + index + ".png")
          );
          inlineImages[cid] = imgBlob;
          attachments.push(imgBlob);
          imagesHtml += '<p><img src="cid:' + cid + '" alt="Zaświaty" style="max-width:100%;border-radius:8px;margin-bottom:10px;"></p>';
        }
      } catch(e) { console.error("Błąd obrazka " + index + ": " + e.message); }
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
      } catch(e) { console.error("Błąd wideo " + index + ": " + e.message); }
    });
  }
  if (data.debug_txt && data.debug_txt.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.debug_txt.base64),
        data.debug_txt.content_type || "text/plain",
        data.debug_txt.filename     || "_.txt"
      ));
    } catch(e) { console.error("Błąd debug TXT: " + e.message); }
  }

  attachLogFiles(data, attachments, attachedNames);

  var htmlBody = "<div>" + (data.reply_html || "<p>(Brak treści)</p>") + imagesHtml + "</div>";
  try {
    msg.reply("", { htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Autoresponder zza światowy" });
    console.log("Wysłano smierc (etap " + newEtap + ") do " + recipient);
  } catch(e) {
    try {
      MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Autoresponder zza światowy" });
    } catch(e2) { console.error("sendEmail() też fail: " + e2.message); }
  }
}

function executeScrabbleMailSend(data, recipient, subject, msg) {
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) { return; }
  var inlineImages = {};
  var attachments  = [];
  var attachedNames = {};
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
  attachLogFiles(data, attachments, attachedNames);
  var htmlBody = (data.reply_html || "") +
    (inlineImages["scrabble_cid"] ? '<p><img src="cid:scrabble_cid" alt="Scrabble" style="max-width:100%;"></p>' : "");
  try {
    msg.reply("", { htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Scrabble – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, inlineImages: inlineImages, attachments: attachments, name: "Scrabble – Autoresponder" });
  }
}

function executeDociekliwyMailSend(data, recipient, subject, msg) {
  if (!data) return;
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) { return; }
  var attachments = [];
  var attachedNames = {};
  var docxList = data.docx_list || [];
  for (var i = 0; i < docxList.length; i++) {
    addBase64Attachment(attachments, attachedNames, docxList[i], "analiza_" + (i + 1) + ".html", "text/html");
  }
  addBase64Attachment(attachments, attachedNames, data.gra_html, "analiza.html", "text/html");
  attachLogFiles(data, attachments, attachedNames);
  var htmlBody = data.reply_html || "<p>Dociekliwy powtórzeń w załączniku.</p>";
  try {
    msg.reply("", { htmlBody: htmlBody, attachments: attachments, name: "Doprecyzuj dociekliwy - autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, attachments: attachments, name: "Doprecyzuj dociekliwy - autoresponder" });
  }
}

function executeEmocjeMailSend(data, recipient, subject, msg) {
  if (!data) return;
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) { return; }
  var attachments = [];
  var attachedNames = {};
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
  attachLogFiles(data, attachments, attachedNames);
  var htmlBody = data.reply_html || "<p>Analiza emocjonalna w załącznikach.</p>";
  try {
    msg.reply("", { htmlBody: htmlBody, attachments: attachments, name: "Analiza Emocjonalna – Autoresponder" });
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject, htmlBody: htmlBody, attachments: attachments, name: "Analiza Emocjonalna – Autoresponder" });
  }
}

function executeNawiazanieMailSend(data, recipient, subject, msg, senderName) {
  if (!data || !data.has_history || !data.reply_html) return;
  var adminEmail = PropertiesService.getScriptProperties().getProperty("ADMIN_EMAIL");
  if (adminEmail && recipient.toLowerCase() === adminEmail.toLowerCase()) { return; }
  var attachments = [];
  try {
    msg.reply("", { htmlBody: data.reply_html, name: senderName || "Nawiązanie – Autoresponder", attachments: attachments });
    console.log("Wysłano nawiązanie -> " + recipient);
  } catch (e) {
    MailApp.sendEmail({ to: recipient, subject: "RE: " + subject + " [nawiązanie]", htmlBody: data.reply_html, name: senderName || "Nawiązanie – Autoresponder", attachments: attachments });
  }
}

// ── Wywołanie backendu ────────────────────────────────────────────────────────
/**
 * _callBackend — wysyła POST do Render i NATYCHMIAST zwraca { accepted: true }
 * jeśli backend odpowiedział 200.
 *
 * Nowy backend zwraca: {"status": "accepted", "message_id": "...", "sections": [...]}
 * GAS nie czeka na wyniki sekcji — backend sam wyśle maile w tle.
 */
function _callBackend(sender, senderName, subject, body, searchText, url, msgId,
                      wantsScrabble, wantsDociekliwy, wantsEmocje,
                      wantsGeneratorPdf, wantsSmierc, smircData,
                      attachments, previousBody, previousSubject,
                      isBiz, isAllowed, isKnownSender, containsKeyword,
                      containsKeyword1, containsKeyword2, containsKeyword3, containsKeyword4,
                      containsFlagaTest, containsKeywordGeneratorPdf, containsKeywordSmierc,
                      containsJoker, shouldSendSmierc,
                      disableFlux, threadId, retryResponders, attemptCount) {
  var KEYWORDS = _getListFromProps("KEYWORDS");
  var KEYWORDS1 = _getListFromProps("KEYWORDS1");
  var KEYWORDS2 = _getListFromProps("KEYWORDS2");
  var KEYWORDS3 = _getListFromProps("KEYWORDS3");
  var KEYWORDS4 = _getListFromProps("KEYWORDS4");
  var KEYWORDS_JOKER = _getListFromProps("KEYWORDS_JOKER");
  var KEYWORDS_GENERATOR_PDF = _getListFromProps("KEYWORDS_GENERATOR_PDF");
  var KEYWORDS_SMIERC = _getListFromProps("KEYWORDS_SMIERC");
  var FLAGA_TEST = _getListFromProps("FLAGA_TEST");
  var secret  = PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET");

  var payload = {
    msg_id:              msgId            || "",
    message_id:          msgId            || "",
    sender:              sender,
    sender_name:         senderName        || "",
    subject:             subject,
    body:                body,
    // ── Flagi respondentów ──────────────────────────────────────────────────
    wants_scrabble:      wantsScrabble      ? true : false,
    wants_analiza:       wantsDociekliwy    ? true : false,
    wants_emocje:        wantsEmocje        ? true : false,
    wants_generator_pdf: wantsGeneratorPdf  ? true : false,
    wants_smierc:        wantsSmierc        ? true : false,
    // wants_zwykly i wants_biznes — Render czyta je wprost z payload-u
    wants_zwykly:        (isBiz || isAllowed || isKnownSender || containsKeyword || containsJoker) ? true : false,
    wants_biznes:        (isBiz || containsKeyword1 || containsJoker) ? true : false,
    // ── Status nadawcy — Render używa isBiz/isAllowed/isKnownSender ────────
    isBiz:               isBiz              ? true : false,
    isAllowed:           isAllowed          ? true : false,
    isKnownSender:       isKnownSender      ? true : false,
    // ── Flagi keywordów ─────────────────────────────────────────────────────
    contains_keyword:    containsKeyword    ? true : false,
    contains_keyword1:   containsKeyword1   ? true : false,
    contains_keyword2:   containsKeyword2   ? true : false,
    contains_keyword3:   containsKeyword3   ? true : false,
    contains_keyword4:   containsKeyword4   ? true : false,
    contains_flaga_test: containsFlagaTest   ? true : false,
    contains_keyword_generator_pdf: containsKeywordGeneratorPdf ? true : false,
    contains_keyword_smierc: containsKeywordSmierc ? true : false,
    contains_keyword_joker: containsJoker ? true : false,
    wants_text_reply:    (isBiz || isAllowed || isKnownSender || containsKeyword || containsJoker || wantsScrabble || wantsDociekliwy || wantsEmocje || wantsGeneratorPdf) ? true : false,
    attachments:         attachments        || [],
    previous_body:       previousBody       || null,
    previous_subject:    previousSubject    || null,
    save_to_drive:       true,
    disable_flux:        disableFlux       ? true : false,
    matched_keywords: {
      keywords:      _findMatchingKeywords(searchText, KEYWORDS),
      keywords1:     _findMatchingKeywords(searchText, KEYWORDS1),
      keywords2:     _findMatchingKeywords(searchText, KEYWORDS2),
      keywords3:     _findMatchingKeywords(searchText, KEYWORDS3),
      keywords4:     _findMatchingKeywords(searchText, KEYWORDS4),
      keywords_flaga: _findMatchingKeywords(searchText, FLAGA_TEST),
      keywords_joker:_findMatchingKeywords(searchText, KEYWORDS_JOKER),
      keywords_smierc:_findMatchingKeywords(searchText, KEYWORDS_SMIERC),
    },
    thread_id:           threadId         || null,
    retry_responders:    retryResponders  || [],
    attempt_count:       attemptCount     || 1,
    skip_save_to_history: (!isAllowed && !isKnownSender && !containsKeyword && !containsKeyword1 && !containsKeyword2 && !containsKeyword3 && !containsKeyword4 && !containsKeywordGeneratorPdf && !containsJoker && !containsKeywordSmierc && !shouldSendSmierc) ? true : false,
  };

  // Dodaj dane śmierci do payload
  if (smircData) {
    payload.etap         = smircData.etap         || 1;
    payload.data_smierci = smircData.data_smierci || "nieznanego dnia";
    payload.historia     = smircData.historia     || [];
  }

  // Przekaż ID arkusza śmierci — Render sam zapisze wyniki po przetworzeniu
  var smircHistSheetId = PropertiesService.getScriptProperties().getProperty("SMIERC_HISTORY_SHEET_ID");
  if (smircHistSheetId) {
    payload.smierc_sheet_id = smircHistSheetId;
  }

  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            JSON.stringify(payload),
    muteHttpExceptions: true,
    headers:            secret ? { "X-Webhook-Secret": secret } : {},
    deadline:           25,  // Render może mieć cold start — czekamy 25s
  };

  try {
    var resp = UrlFetchApp.fetch(url, options);
    var code = resp.getResponseCode();

    if (code === 200) {
      var text = resp.getContentText();
      try {
        var json = JSON.parse(text);
        // Nowy backend: status = "accepted" — nie zwraca wyników sekcji
        if (json.status === "accepted") {
          console.log("Webhook — ACCEPTED message_id=" + json.message_id + " sections=" + (json.sections || []).join(","));
          return { accepted: true, message_id: json.message_id };
        }
        // Stary backend (fallback) — zwraca pełny JSON z sekcjami
        console.log("Webhook — odpowiedź OK (200) [legacy mode]");
        return { accepted: true, json: json };
      } catch (e) {
        console.error("Błąd parsowania JSON z backendu: " + e.message);
        _reportBackendError(payload.sender, payload.subject, "Błąd parsowania JSON: " + e.message + " | odpowiedź: " + text);
        return null;
      }
    }
    var errMsg = "Backend zwrócił kod " + code;
    console.error(errMsg);
    _reportBackendError(payload.sender, payload.subject, errMsg + " | body: " + resp.getContentText());
  } catch (e) {
    var errMsg2 = "Błąd połączenia z backendem: " + e.message;
    console.error(errMsg2);
    _reportBackendError(payload.sender, payload.subject, errMsg2);
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
        line.match(/^(wt|sr|czw|pt|sob|niedz|pon)\.,?\s+\d/i) ||
        line.match(/^(On|W dniu|Am)\s+.+wrote:/i) ||
        line.match(/^Dnia\s+\d/i) ||
        line.match(/napisał\(a\):/i)) break;
    result.push(lines[i]);
  }
  return result.join("\n").trim();
}

// ── Etykiety zamiast oznaczania jako przeczytane ──────────────────────────────
function _getProcessedLabel() {
  return GmailApp.getUserLabelByName("autoresponder-processed") ||
         GmailApp.createLabel("autoresponder-processed");
}

function _hasProcessedLabel(thread) {
  var labels = thread.getLabels();
  var target = _getProcessedLabel();
  for (var i = 0; i < labels.length; i++) {
    if (labels[i].getId() === target.getId()) {
      return true;
    }
  }
  return false;
}

function _markAsProcessed(thread) {
  thread.addLabel(_getProcessedLabel());
}

// ════════════════════════════════════════════════════════════════════════════
// GŁÓWNA PĘTLA
// ════════════════════════════════════════════════════════════════════════════

function __AAA_processEmails() {
  var props      = PropertiesService.getScriptProperties();
  var webhookUrl = props.getProperty("WEBHOOK_URL");
  if (!webhookUrl) { console.error("Brak WEBHOOK_URL!"); return; }

  var BIZ_LIST                   = _getListFromProps("BIZ_LIST");
  var ALLOWED_LIST               = _getListFromProps("ALLOWED_LIST");
  var ALLOWED_LIST_GENERATOR_PDF = _getListFromProps("ALLOWED_LIST_GENERATOR_PDF");
  var KEYWORDS                   = _getListFromProps("KEYWORDS");
  var KEYWORDS1                  = _getListFromProps("KEYWORDS1");
  var KEYWORDS2                  = _getListFromProps("KEYWORDS2");
  var KEYWORDS3                  = _getListFromProps("KEYWORDS3");
  var KEYWORDS4                  = _getListFromProps("KEYWORDS4");
  var KEYWORDS_JOKER             = _getListFromProps("KEYWORDS_JOKER");
  var KEYWORDS_GENERATOR_PDF     = _getListFromProps("KEYWORDS_GENERATOR_PDF");
  var KEYWORDS_SMIERC            = _getListFromProps("KEYWORDS_SMIERC");
  var FLAGA_TEST                 = _getListFromProps("FLAGA_TEST");
  var BANNED_EMAILS              = _getListFromProps("BANNED_EMAILS");
  var DATA_SMIERCI               = props.getProperty("DATA_SMIERCI") || "nieznanego dnia";
  var ADMIN_EMAIL                = props.getProperty("ADMIN_EMAIL");

  var maskMode      = false;
  var knownSenders  = _getKnownSenders();

  console.log("Znani nadawcy: " + knownSenders.length);

  var threads = GmailApp.getInboxThreads(0, 80).reverse();
  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    if (_hasProcessedLabel(thread)) continue;

    var webhookCalled = false;
    var messages   = thread.getMessages();
    var msg        = messages[messages.length - 1];
    var msgId      = msg.getId();
    var fromRaw    = msg.getFrom();
    var fromEmail  = extractEmail(fromRaw).toLowerCase();

    if (ADMIN_EMAIL && fromEmail === ADMIN_EMAIL.toLowerCase()) {
      console.log("🔒 BLOKADA ADMIN_EMAIL: " + fromEmail);
      _markAsProcessed(thread);
      continue;
    }

    // Blokada adresów systemowych (no-reply, kalendarze itp.) i listy BANNED_EMAILS
    if (_isBannedSender(fromEmail, BANNED_EMAILS)) {
      console.log("🚫 BANNED: " + fromEmail + " — pomijam");
      _markAsProcessed(thread);
      continue;
    }

    var senderName = "";
    var nameMatch  = fromRaw.match(/^"?([^"<]+)"?\s*</);
    if (nameMatch) senderName = nameMatch[1].trim();
    var subject    = msg.getSubject();

    // [FIX v13] Pobieranie treści — priorytet Plain Text, fallback do HTML
    // Rozwiązuje błąd "Pusty body emaila" dla maili HTML-only (np. z Gmaila mobilnego)
    var rawPlain = msg.getPlainBody() || "";
    if (!rawPlain.trim()) {
      // Plain Text pusty — próbujemy wyciągnąć tekst z HTML body
      var rawHtml = msg.getBody() || "";
      rawPlain = extractPlainTextFromHtml(rawHtml);
    }
    if (!rawPlain.trim()) {
      // Ostateczny bezpiecznik — mail zawiera wyłącznie grafikę lub załączniki
      rawPlain = "(Wiadomość zawiera tylko grafikę lub załączniki)";
    }
    var plainBody  = _stripQuotedText(rawPlain);
    var searchText = plainBody + " " + subject;

    // ── FLAGI GLOBALNE ────────────────────────────────────────────────────────
    var isBiz         = BIZ_LIST.indexOf(fromEmail) !== -1;
    var isAllowed     = ALLOWED_LIST.indexOf(fromEmail) !== -1;
    var isKnownSender = knownSenders.indexOf(fromEmail) !== -1;

    var smircData      = _getSmircData(fromEmail);
    var isSmierc       = smircData !== null;
    var shouldSendZwykly = isAllowed || isKnownSender;
    var shouldSendSmierc = isSmierc;
    var containsFlagaTest = _containsAny(searchText, FLAGA_TEST);

    var isNewMsg = _isNewMessage(subject);
    console.log("DEBUG: isNewMsg=" + isNewMsg + " from=" + fromEmail +
                " | zwykly=" + shouldSendZwykly + " | smierc=" + shouldSendSmierc);

    // ── RE/FWD ────────────────────────────────────────────────────────────────
    if (!isNewMsg) {
      var flowRow = {
        fromEmail: fromEmail, subject: subject, isNewMsg: isNewMsg,
        KEYWORDS: false, KEYWORDS1: false, KEYWORDS2: false, KEYWORDS3: false,
        KEYWORDS4: false, KEYWORDS_GENERATOR_PDF: false, KEYWORDS_SMIERC: false,
        JOKER: false,
        lista_smiert: isSmierc, lista_historia: shouldSendZwykly,
        flaga_test: containsFlagaTest,
        wysylka: shouldSendSmierc || shouldSendZwykly,
        action: (shouldSendSmierc || shouldSendZwykly) ? "RE_WYSYLKA" : "RE_POMINIETO",
        notes: "RE/FWD"
      };
      _appendPrzeplywSheetRow(flowRow);

      console.log("Odpowiedź (RE:/FWD:) od: " + fromEmail);

      if (shouldSendSmierc || shouldSendZwykly) {
        _markAsProcessed(thread);
        if (webhookCalled) { continue; }
        webhookCalled = true;

        var previousDataReply = findLastMessageBySender(fromEmail);

        // ── Zapisz ODEBRANO przed callем ──────────────────────────────────────
        _saveOdebranoToHistory(msgId, fromEmail, subject, plainBody);

        var responseReply = _callBackend(
          fromEmail, senderName, subject, plainBody, plainBody, webhookUrl, msgId,
          false, false, false, false, shouldSendSmierc,
          smircData, [],
          previousDataReply ? previousDataReply.body    : null,
          previousDataReply ? previousDataReply.subject : null,
          isBiz, isAllowed, isKnownSender,
          shouldSendZwykly, false, false, false, false,
          containsFlagaTest, false, false,
          false, shouldSendSmierc, containsFlagaTest,
          null, [], 1
        );

        if (responseReply && responseReply.accepted) {
          console.log("RE/FWD zaakceptowany przez backend — maile wyśle Render");
          // Jeśli backend ma legacy json (stary tryb) — wyślij sami
          if (responseReply.json) {
            var rj = responseReply.json;
            if (shouldSendSmierc && rj.smierc) {
              var newEtapReply = rj.smierc.nowy_etap || smircData.etap;
              executeSmircMailSend(sectionWithLogs(rj.smierc, rj), fromEmail, subject, msg, newEtapReply);
              _updateSmircData(fromEmail, newEtapReply, plainBody, rj.smierc.reply_html, msg.getId());
            }
            if (shouldSendZwykly && rj.zwykly) {
              executeMailSend(rj.zwykly, fromEmail, subject, msg, "Tyler Durden – Autoresponder");
            }
          }
        } else if (responseReply === null) {
          console.warn("Backend nie odpowiedział dla: " + fromEmail);
        }
        saveToHistory(fromEmail, subject, plainBody);
        break;
      } else {
        _markAsProcessed(thread);
      }
      continue;
    }

    // ── NOWA WIADOMOŚĆ ────────────────────────────────────────────────────────
    var isAllowedGeneratorPdf       = ALLOWED_LIST_GENERATOR_PDF.indexOf(fromEmail) !== -1;
    var containsKeywordGeneratorPdf = _containsAny(searchText, KEYWORDS_GENERATOR_PDF);
    var wantsGeneratorPdf           = isAllowedGeneratorPdf || containsKeywordGeneratorPdf;

    // ── JOKER ────────────────────────────────────────────────────────────────
    var containsJoker = _containsAny(searchText, KEYWORDS_JOKER);
    if (containsJoker) {
      console.log("🃏 JOKER! Aktywacja dla: " + fromEmail);

      if (!isSmierc) {
        if (_createSmircSheetForEmail(fromEmail, DATA_SMIERCI)) {
          smircData        = _getSmircData(fromEmail) || { etap: 1, data_smierci: DATA_SMIERCI, historia: [] };
          isSmierc         = true;
          shouldSendSmierc = true;
        }
      }
      _markAsProcessed(thread);
      if (webhookCalled) { continue; }
      webhookCalled = true;

      // ── Zapisz ODEBRANO przed callем ──────────────────────────────────────
      _saveOdebranoToHistory(msgId, fromEmail, subject, plainBody);

      // 🃏 JOKER — wszystkie respondery włączone, wszystkie keyword-flagi true
      var jokerPrevData = findLastMessageBySender(fromEmail);
      var responseJoker = _callBackend(
        fromEmail, senderName, subject, plainBody, plainBody, webhookUrl, msgId,
        /*wantsScrabble*/           true,
        /*wantsDociekliwy*/         true,
        /*wantsEmocje*/             true,
        /*wantsGeneratorPdf*/       true,
        /*wantsSmierc*/             shouldSendSmierc,
        smircData, getAllAttachments(msg),
        jokerPrevData ? jokerPrevData.body    : null,
        jokerPrevData ? jokerPrevData.subject : null,
        /*isBiz*/                   true,
        /*isAllowed*/               true,
        /*isKnownSender*/           true,
        /*containsKeyword*/         true,
        /*containsKeyword1*/        true,
        /*containsKeyword2*/        true,
        /*containsKeyword3*/        true,
        /*containsKeyword4*/        true,
        containsFlagaTest,
        /*containsKeywordGeneratorPdf*/ true,
        /*containsKeywordSmierc*/   containsKeywordSmierc,
        /*containsJoker*/           true,
        /*shouldSendSmierc*/        shouldSendSmierc,
        /*disableFlux*/             containsFlagaTest,
        null, [], 1
      );

      if (responseJoker && responseJoker.accepted) {
        console.log("🃏 JOKER: zaakceptowany przez backend — maile wyśle Render");
        // Legacy fallback
        if (responseJoker.json) {
          var jj = responseJoker.json;
          if (jj.biznes)        { executeMailSend(sectionWithLogs(jj.biznes, jj), fromEmail, subject, msg, "Notariusz – Informacja"); }
          if (jj.zwykly)        { executeMailSend(sectionWithLogs(jj.zwykly, jj), fromEmail, subject, msg, "Bot Tylera"); }
          if (jj.scrabble)      { executeScrabbleMailSend(sectionWithLogs(jj.scrabble, jj), fromEmail, subject, msg); }
          if (jj.dociekliwy)    { executeDociekliwyMailSend(sectionWithLogs(jj.dociekliwy, jj), fromEmail, subject, msg); }
          if (jj.emocje)        { executeEmocjeMailSend(sectionWithLogs(jj.emocje, jj), fromEmail, subject, msg); }
          if (jj.nawiazanie)    { executeNawiazanieMailSend(sectionWithLogs(jj.nawiazanie, jj), fromEmail, subject, msg, "Bot Tylera"); }
          if (jj.generator_pdf) { executeGeneratorPdfMailSend(sectionWithLogs(jj.generator_pdf, jj), fromEmail, subject, msg); }
          if (shouldSendSmierc && jj.smierc) {
            var newEtapJoker = jj.smierc.nowy_etap || smircData.etap;
            executeSmircMailSend(sectionWithLogs(jj.smierc, jj), fromEmail, subject, msg, newEtapJoker);
            _updateSmircData(fromEmail, newEtapJoker, plainBody, jj.smierc.reply_html, msg.getId());
          }
        }
      }
      saveToHistory(fromEmail, subject, plainBody);
      break;
    }

    // ── SMIERC kontynuacja ────────────────────────────────────────────────────
    if (shouldSendSmierc) {
      console.log("SMIERC kontynuacja: " + fromEmail + " etap=" + smircData.etap);

      // Zapisz do arkusza decyzji
      _appendPrzeplywSheetRow({
        fromEmail: fromEmail, subject: subject, isNewMsg: isNewMsg,
        KEYWORDS: false, KEYWORDS1: false, KEYWORDS2: false, KEYWORDS3: false,
        KEYWORDS4: false, KEYWORDS_GENERATOR_PDF: false, KEYWORDS_SMIERC: false,
        JOKER: false,
        lista_smiert: true, lista_historia: shouldSendZwykly,
        flaga_test: containsFlagaTest,
        wysylka: true,
        action: "SMIERC_KONTYNUACJA",
        notes: "etap=" + (smircData ? smircData.etap : "?")
      });

      _markAsProcessed(thread);

      var previousData2 = findLastMessageBySender(fromEmail);
      if (webhookCalled) { continue; }
      webhookCalled = true;

      // ── Zapisz ODEBRANO przed callем ──────────────────────────────────────
      _saveOdebranoToHistory(msgId, fromEmail, subject, plainBody);

      var response2 = _callBackend(
        fromEmail, senderName, subject, plainBody, plainBody, webhookUrl, msgId,
        false, false, false, false, true,
        smircData, [],
        previousData2 ? previousData2.body    : null,
        previousData2 ? previousData2.subject : null,
        isBiz, isAllowed, isKnownSender,
        false, false, false, false, false,
        containsFlagaTest, false, false,
        false, true, containsFlagaTest,
        null, [], 1
      );

      if (response2 && response2.accepted) {
        console.log("SMIERC kontynuacja: zaakceptowany przez backend");
        // Legacy fallback
        if (response2.json) {
          if (response2.json.smierc) {
            var newEtap2 = response2.json.smierc.nowy_etap || smircData.etap;
            executeSmircMailSend(sectionWithLogs(response2.json.smierc, response2.json), fromEmail, subject, msg, newEtap2);
            _updateSmircData(fromEmail, newEtap2, plainBody, response2.json.smierc.reply_html, msg.getId());
          }
          if (shouldSendZwykly && response2.json.zwykly) {
            executeMailSend(response2.json.zwykly, fromEmail, subject, msg, "Tyler Durden – Autoresponder");
          }
        }
      }
      saveToHistory(fromEmail, subject, plainBody);
      break;
    }

    // ── SMIERC start ──────────────────────────────────────────────────────────
    var containsKeywordSmierc = _containsAny(searchText, KEYWORDS_SMIERC);
    var flowRow2 = {
      fromEmail: fromEmail, subject: subject, isNewMsg: isNewMsg,
      KEYWORDS: false, KEYWORDS1: false, KEYWORDS2: false, KEYWORDS3: false,
      KEYWORDS4: false, KEYWORDS_GENERATOR_PDF: false,
      KEYWORDS_SMIERC: false, JOKER: false,
      lista_smiert: isSmierc, lista_historia: shouldSendZwykly,
      flaga_test: containsFlagaTest, wysylka: false, action: "", notes: ""
    };

    if (containsKeywordSmierc) {
      if (_createSmircSheetForEmail(fromEmail, DATA_SMIERCI)) {
        smircData        = _getSmircData(fromEmail);
        isSmierc         = true;
        shouldSendSmierc = true;
        flowRow2.lista_smiert = true;
        console.log("SMIERC start: " + fromEmail);
      }
    }

    var containsKeyword        = _containsAny(searchText, KEYWORDS);
    var containsKeyword1       = _containsAny(searchText, KEYWORDS1);
    var containsKeyword2       = _containsAny(searchText, KEYWORDS2);
    var containsKeyword3       = _containsAny(searchText, KEYWORDS3);
    var containsKeyword4       = _containsAny(searchText, KEYWORDS4);
    containsFlagaTest          = _containsAny(searchText, FLAGA_TEST);

    flowRow2.KEYWORDS = containsKeyword;
    flowRow2.KEYWORDS1 = containsKeyword1;
    flowRow2.KEYWORDS2 = containsKeyword2;
    flowRow2.KEYWORDS3 = containsKeyword3;
    flowRow2.KEYWORDS4 = containsKeyword4;
    flowRow2.KEYWORDS_GENERATOR_PDF = containsKeywordGeneratorPdf;
    flowRow2.KEYWORDS_SMIERC = containsKeywordSmierc;
    flowRow2.JOKER = containsJoker;
    flowRow2.flaga_test = containsFlagaTest;

    if (containsKeyword) shouldSendZwykly = true;
    flowRow2.lista_historia = shouldSendZwykly;

    if (containsJoker) {
      flowRow2.wysylka = true; flowRow2.action = "JOKER_WYSYLKA"; flowRow2.notes = "JOKER";
    } else if (shouldSendSmierc) {
      flowRow2.wysylka = true; flowRow2.action = "SMIERC_WYSYLKA"; flowRow2.notes = "SMIERC";
    } else if (!isBiz && !shouldSendZwykly && !containsKeyword1 && !containsKeyword2 && !containsKeyword3 &&
               !containsKeyword4 && !containsKeywordGeneratorPdf && !containsJoker && !containsKeywordSmierc && !shouldSendSmierc) {
      flowRow2.wysylka = false; flowRow2.action = "POMINIETO"; flowRow2.notes = "brak warunków wysyłki";
      _appendPrzeplywSheetRow(flowRow2);
      _markAsProcessed(thread);
      continue;
    } else {
      flowRow2.wysylka = true; flowRow2.action = "WYSYLKA"; flowRow2.notes = "zwykły lub keyword";
    }
    _appendPrzeplywSheetRow(flowRow2);

    var shouldSaveHistory = isKnownSender || isAllowed || containsKeyword || containsKeyword1 || containsKeyword2 || containsKeyword3 || containsKeyword4 || containsKeywordGeneratorPdf || containsJoker || containsKeywordSmierc || shouldSendSmierc;

    var combinedKeywords = KEYWORDS.concat(KEYWORDS1).concat(KEYWORDS2).concat(KEYWORDS3)
      .concat(KEYWORDS4).concat(KEYWORDS_JOKER).concat(KEYWORDS_SMIERC).filter(Boolean);
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

    _markAsProcessed(thread);
    if (webhookCalled) { continue; }
    webhookCalled = true;

    var disableFlux = containsFlagaTest && (isBiz || isAllowed || isKnownSender || containsKeyword || containsKeyword2 || containsKeyword3 || containsKeyword4 || shouldSendSmierc);

    // ── Zapisz ODEBRANO do Sheets PRZED callем do Render ─────────────────────
    _saveOdebranoToHistory(msgId, fromEmail, subject, sanitizedBody);

    var response = _callBackend(
      fromEmail, senderName, subject, sanitizedBody, plainBody, webhookUrl, msgId,
      containsKeyword2, containsKeyword3, containsKeyword4,
      wantsGeneratorPdf, shouldSendSmierc, smircData, allAttachments,
      previousBody, previousSubject,
      isBiz, isAllowed, isKnownSender,
      containsKeyword, containsKeyword1, containsKeyword2, containsKeyword3, containsKeyword4,
      containsFlagaTest, containsKeywordGeneratorPdf, containsKeywordSmierc,
      containsJoker, shouldSendSmierc,
      disableFlux, null, [], 1
    );

    if (response && response.accepted) {
      console.log("✅ Webhook zaakceptowany — Render wyśle maile w tle");
      // Legacy fallback: jeśli backend zwrócił pełny JSON (stary tryb)
      if (response.json) {
        var json = response.json;
        if (json.biznes && (isBiz || containsKeyword1 || containsJoker)) {
          executeMailSend(sectionWithLogs(json.biznes, json), fromEmail, subject, msg, "Notariusz – Informacja");
        }
        if (json.zwykly && shouldSendZwykly) {
          executeMailSend(sectionWithLogs(json.zwykly, json), fromEmail, subject, msg, "Tyler Durden – Autoresponder");
        }
        if (containsKeyword2 && json.scrabble) {
          executeScrabbleMailSend(sectionWithLogs(json.scrabble, json), fromEmail, subject, msg);
        }
        if (containsKeyword3 && json.analiza) {
          executeDociekliwyMailSend(sectionWithLogs(json.analiza, json), fromEmail, subject, msg);
        }
        if (containsKeyword4 && json.emocje) {
          executeEmocjeMailSend(sectionWithLogs(json.emocje, json), fromEmail, subject, msg);
        }
        if (json.nawiazanie) {
          executeNawiazanieMailSend(sectionWithLogs(json.nawiazanie, json), fromEmail, subject, msg);
        }
        if (wantsGeneratorPdf && json.generator_pdf) {
          executeGeneratorPdfMailSend(sectionWithLogs(json.generator_pdf, json), fromEmail, subject, msg);
        }
        if (shouldSendSmierc && json.smierc) {
          var newEtap = json.smierc.nowy_etap || smircData.etap;
          executeSmircMailSend(sectionWithLogs(json.smierc, json), fromEmail, subject, msg, newEtap);
          _updateSmircData(fromEmail, newEtap, sanitizedBody, json.smierc.reply_html, msg.getId());
        }
      }
      if (shouldSaveHistory) {
        saveToHistory(fromEmail, subject, sanitizedBody);
      }
      break;
    } else {
      console.warn("Backend nie odpowiedział dla: " + fromEmail);
    }
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function extractEmail(fromHeader) {
  if (!fromHeader) return "";
  var match = fromHeader.match(/<([^>]+)>/);
  if (match && match[1]) {
    return match[1];
  }
  return fromHeader.split(" ")[0] || "";
}


// ════════════════════════════════════════════════════════════════════════════
// BANNED SENDERS
// ════════════════════════════════════════════════════════════════════════════

/**
 * _isBannedSender — zwraca true jeśli adres jest systemowy lub na liście BANNED_EMAILS.
 *
 * Automatycznie blokowane wzorce:
 *   no-reply@, noreply@, noreply-, mailer-daemon, postmaster@,
 *   calendar-notification@, notifications@, notification@,
 *   bounce@, bounces@, donotreply@, do-not-reply@
 *
 * Ręczna lista dodatkowych adresów: Script Properties → BANNED_EMAILS
 *   Przykład: calendar-notification@google.com,info@newsletter.pl
 */
function _isBannedSender(email, bannedList) {
  if (!email) return false;
  var e = email.toLowerCase().trim();

  // Ręczna lista z Script Properties
  if (bannedList && bannedList.length) {
    for (var i = 0; i < bannedList.length; i++) {
      if (bannedList[i] && e === bannedList[i].toLowerCase().trim()) return true;
    }
  }

  // Wzorce automatyczne
  var patterns = [
    "no-reply@", "noreply@", "noreply-",
    "mailer-daemon", "postmaster@",
    "calendar-notification@",
    "notifications@", "notification@",
    "bounce@", "bounces@",
    "donotreply@", "do-not-reply@"
  ];
  for (var j = 0; j < patterns.length; j++) {
    if (e.indexOf(patterns[j]) !== -1) return true;
  }
  return false;
}



function keepAlive() {
  var url = PropertiesService.getScriptProperties().getProperty("WEBHOOK_URL");
  if (!url) return;
  try {
    UrlFetchApp.fetch(url.replace("/webhook", "/"), { muteHttpExceptions: true });
  } catch (e) {
    console.warn("keepAlive fetch failed: " + e.message);
  }
}
