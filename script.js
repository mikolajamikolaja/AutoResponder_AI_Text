/**
 * processEmailsFinal - Google Apps Script
 * Script Properties:
 *   WEBHOOK_URL      - URL do backendu (Render)
 *   WEBHOOK_SECRET   - (opcjonalnie)
 *   BIZ_LIST         - emails → odpowiedź biznesowa
 *   ALLOWED_LIST     - emails → odpowiedź emocjonalna
 *   KEYWORDS         - słowa kluczowe → biz + zwykly
 *   KEYWORDS1        - drugi zestaw → biz + zwykly
 *   KEYWORDS2        - słowa kluczowe → scrabble (obrazek)
 *   KEYWORDS3        - słowa kluczowe → analiza powtórzeń (DOCX)
 *   KEYWORDS4        - słowa kluczowe → analiza emocjonalna (wykresy PNG)
 *   KEYWORDS_JOKER   - słowa testowe → aktywuje WSZYSTKIE respondery (np. joker)
 */

function _getListFromProps(name) {
  var props = PropertiesService.getScriptProperties();
  var raw = props.getProperty(name) || "";
  return raw.split(",").map(function(s){ return s.trim().toLowerCase(); }).filter(Boolean);
}

function escapeRegExp(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function removeKeywordsFromText(text, keywords, maskMode) {
  if (!text || !keywords || !keywords.length) return text;
  var sanitized = text;
  var sorted = keywords.slice().filter(Boolean).sort(function(a, b){ return b.length - a.length; });
  sorted.forEach(function(k) {
    if (!k) return;
    var re = new RegExp(escapeRegExp(k), "gi");
    sanitized = sanitized.replace(re, maskMode ? "[REDACTED]" : "");
  });
  sanitized = sanitized.replace(/[ \t]{2,}/g, " ");
  sanitized = sanitized.replace(/\n{3,}/g, "\n\n");
  return sanitized.trim();
}

// ── Pobierz załączniki DOCX/ODT/PDF/TXT ──────────────────────────────────────
function getAllAttachments(msg) {
  var attachments = msg.getAttachments();
  if (!attachments || !attachments.length) return [];

  var docTypes = [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.text-template",
    "application/pdf",
    "text/plain",
  ];

  var result = [];
  for (var i = 0; i < attachments.length; i++) {
    var att  = attachments[i];
    var mime = att.getContentType();
    var name = att.getName().toLowerCase();

    var isDoc = docTypes.indexOf(mime) !== -1 ||
                name.endsWith(".docx") ||
                name.endsWith(".doc")  ||
                name.endsWith(".odt")  ||
                name.endsWith(".ott")  ||
                name.endsWith(".pdf")  ||
                name.endsWith(".txt");

    if (isDoc) {
      result.push({
        base64: Utilities.base64Encode(att.getBytes()),
        name:   att.getName()
      });
    }
  }
  return result;
}

// ── Wysyłka: analiza powtórzeń DOCX ──────────────────────────────────────────
function executeAnalizaMailSend(data, recipient, subject, msg) {
  if (!data) {
    console.warn("Brak danych analizy dla " + recipient);
    return;
  }

  var attachments = [];
  var docxList = data.docx_list || [];

  for (var i = 0; i < docxList.length; i++) {
    var d = docxList[i];
    if (!d || !d.base64) continue;
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(d.base64),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        d.filename || ("analiza_" + (i + 1) + ".docx")
      ));
    } catch (e) {
      console.error("Błąd dekodowania DOCX [" + i + "]: " + e.message);
    }
  }

  var htmlBody = data.reply_html || "<p>Analiza powtórzeń w załączniku.</p>";
  try {
    msg.reply("", {
      htmlBody:    htmlBody,
      attachments: attachments,
      name:        "Analiza Powtórzeń – Autoresponder"
    });
    console.log("Wysłano analizę powtórzeń (" + attachments.length + " DOCX) -> " + recipient);
  } catch (e) {
    console.warn("reply() nie działa, wysyłam nowy mail: " + e.message);
    MailApp.sendEmail({
      to:          recipient,
      subject:     "RE: " + subject,
      htmlBody:    htmlBody,
      attachments: attachments,
      name:        "Analiza Powtórzeń – Autoresponder"
    });
  }
}

// ── Wysyłka: analiza emocjonalna PNG + raporty TXT ────────────────────────────
function executeEmocjeMailSend(data, recipient, subject, msg) {
  if (!data) {
    console.warn("Brak danych emocji dla " + recipient);
    return;
  }

  var attachments = [];

  // Wykresy PNG
  var images = data.images || [];
  for (var i = 0; i < images.length; i++) {
    var img = images[i];
    if (!img || !img.base64) continue;
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(img.base64),
        img.content_type || "image/png",
        img.filename     || ("wykres_" + (i + 1) + ".png")
      ));
    } catch (e) {
      console.error("Błąd wykresu [" + i + "]: " + e.message);
    }
  }

  // Raporty TXT
  var docs = data.docs || [];
  for (var j = 0; j < docs.length; j++) {
    var doc = docs[j];
    if (!doc || !doc.base64) continue;
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(doc.base64),
        doc.content_type || "text/plain",
        doc.filename     || ("raport_" + (j + 1) + ".txt")
      ));
    } catch (e) {
      console.error("Błąd raportu TXT [" + j + "]: " + e.message);
    }
  }

  var htmlBody = data.reply_html || "<p>Analiza emocjonalna w załącznikach.</p>";

  try {
    msg.reply("", {
      htmlBody:    htmlBody,
      attachments: attachments,
      name:        "Analiza Emocjonalna – Autoresponder"
    });
    console.log("Wysłano analizę emocjonalną (" +
      images.length + " PNG, " + docs.length + " TXT) -> " + recipient);
  } catch (e) {
    console.warn("reply() nie działa: " + e.message);
    MailApp.sendEmail({
      to:          recipient,
      subject:     "RE: " + subject,
      htmlBody:    htmlBody,
      attachments: attachments,
      name:        "Analiza Emocjonalna – Autoresponder"
    });
  }
}

// ── Główna funkcja ────────────────────────────────────────────────────────────
function processEmailsFinal() {
  var props = PropertiesService.getScriptProperties();
  var webhookUrl = props.getProperty("WEBHOOK_URL");
  if (!webhookUrl) {
    console.error("Brak WEBHOOK_URL w Script Properties!");
    return;
  }

  var BIZ_LIST       = _getListFromProps("BIZ_LIST");
  var ALLOWED_LIST   = _getListFromProps("ALLOWED_LIST");
  var KEYWORDS       = _getListFromProps("KEYWORDS");
  var KEYWORDS1      = _getListFromProps("KEYWORDS1");
  var KEYWORDS2      = _getListFromProps("KEYWORDS2");
  var KEYWORDS3      = _getListFromProps("KEYWORDS3");
  var KEYWORDS4      = _getListFromProps("KEYWORDS4");
  var KEYWORDS_JOKER = _getListFromProps("KEYWORDS_JOKER");  // ← joker

  var maskMode = false;

  var threads = GmailApp.getInboxThreads(0, 20);
  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    if (!thread.isUnread()) continue;

    var messages   = thread.getMessages();
    var msg        = messages[messages.length - 1];
    var fromRaw    = msg.getFrom();
    var fromEmail  = extractEmail(fromRaw).toLowerCase();
    var plainBody  = msg.getPlainBody();
    var searchText = (plainBody + " " + msg.getSubject()).toLowerCase();

    // ── Flagi ─────────────────────────────────────────────────────────────────
    var isBiz     = BIZ_LIST.indexOf(fromEmail) !== -1;
    var isAllowed = ALLOWED_LIST.indexOf(fromEmail) !== -1;

    var containsKeyword = (
      KEYWORDS.some(function(k){  return k && searchText.indexOf(k) !== -1; }) ||
      KEYWORDS1.some(function(k){ return k && searchText.indexOf(k) !== -1; })
    );
    var containsKeyword2 = KEYWORDS2.some(function(k){
      return k && searchText.indexOf(k) !== -1;
    });
    var containsKeyword3 = KEYWORDS3.some(function(k){
      return k && searchText.indexOf(k) !== -1;
    });
    var containsKeyword4 = KEYWORDS4.some(function(k){
      return k && searchText.indexOf(k) !== -1;
    });

    // ── JOKER — aktywuje wszystkie respondery ─────────────────────────────────
    var containsJoker = KEYWORDS_JOKER.some(function(k){
      return k && searchText.indexOf(k) !== -1;
    });
    if (containsJoker) {
      containsKeyword  = true;
      containsKeyword2 = true;
      containsKeyword3 = true;
      containsKeyword4 = true;
    }

    // Ignoruj jeśli nie spełnia żadnego warunku
    if (!isBiz && !isAllowed && !containsKeyword &&
        !containsKeyword2 && !containsKeyword3 && !containsKeyword4) {
      var label = GmailApp.getUserLabelByName("processed");
      if (!label) label = GmailApp.createLabel("processed");
      thread.addLabel(label);
      continue;
    }

    // ── Wyczyść treść ─────────────────────────────────────────────────────────
    var combinedKeywords = KEYWORDS
      .concat(KEYWORDS1)
      .concat(KEYWORDS2)
      .concat(KEYWORDS3)
      .concat(KEYWORDS4)
      .concat(KEYWORDS_JOKER)  // ← joker też usuwamy z treści
      .filter(Boolean);
    var sanitizedBody = removeKeywordsFromText(plainBody, combinedKeywords, maskMode);

    // ── Pobierz załączniki (KEYWORDS3 lub KEYWORDS4 lub JOKER) ───────────────
    var allAttachments = [];
    if (containsKeyword3 || containsKeyword4) {
      allAttachments = getAllAttachments(msg);
      console.log("Znaleziono załączników: " + allAttachments.length);
    }

    // ── Wywołaj backend ───────────────────────────────────────────────────────
    var response = _callBackend(
      fromEmail,
      msg.getSubject(),
      sanitizedBody,
      webhookUrl,
      containsKeyword2,   // wants_scrabble
      containsKeyword3,   // wants_analiza
      containsKeyword4,   // wants_emocje
      allAttachments      // lista [{base64, name}, ...]
    );

    if (response && response.json) {
      var json = response.json;

      // BIZ_LIST → odpowiedź biznesowa
      if (isBiz && json.biznes) {
        executeMailSend(json.biznes, fromEmail, msg.getSubject(), msg, "Notariusz – Informacja");
      }
      // ALLOWED_LIST → odpowiedź emocjonalna (Tyler)
      if (isAllowed && json.zwykly) {
        executeMailSend(json.zwykly, fromEmail, msg.getSubject(), msg, "Tyler Durden – Autoresponder");
      }
      // KEYWORDS/KEYWORDS1 → obie odpowiedzi
      if (!isBiz && !isAllowed && containsKeyword) {
        if (json.biznes) executeMailSend(json.biznes, fromEmail, msg.getSubject(), msg, "Notariusz – Informacja");
        if (json.zwykly) executeMailSend(json.zwykly, fromEmail, msg.getSubject(), msg, "Tyler Durden – Autoresponder");
      }
      // KEYWORDS2 → scrabble
      if (containsKeyword2 && json.scrabble) {
        executeScrabbleMailSend(json.scrabble, fromEmail, msg.getSubject(), msg);
      }
      // KEYWORDS3 → analiza powtórzeń DOCX
      if (containsKeyword3 && json.analiza) {
        executeAnalizaMailSend(json.analiza, fromEmail, msg.getSubject(), msg);
      }
      // KEYWORDS4 → analiza emocjonalna PNG
      if (containsKeyword4 && json.emocje) {
        executeEmocjeMailSend(json.emocje, fromEmail, msg.getSubject(), msg);
      }
    }

    thread.markRead();
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function extractEmail(fromHeader) {
  var m = fromHeader.match(/<([^>]+)>/);
  if (m) return m[1];
  return fromHeader.split(" ")[0];
}

function _callBackend(sender, subject, body, url,
                      wantsScrabble, wantsAnaliza, wantsEmocje, attachments) {
  var secret = PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET");
  var payload = {
    from:           sender,
    subject:        subject,
    body:           body,
    wants_scrabble: wantsScrabble ? true : false,
    wants_analiza:  wantsAnaliza  ? true : false,
    wants_emocje:   wantsEmocje   ? true : false,
    attachments:    attachments   || []
  };
  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            JSON.stringify(payload),
    muteHttpExceptions: true,
    headers:            secret ? { "X-Webhook-Secret": secret } : {}
  };
  try {
    var resp = UrlFetchApp.fetch(url, options);
    var code = resp.getResponseCode();
    if (code === 200) {
      return { json: JSON.parse(resp.getContentText()) };
    } else {
      console.error("Backend zwrócił kod " + code + ": " + resp.getContentText());
    }
  } catch (e) {
    console.error("Błąd połączenia z backendem: " + e.message);
  }
  return null;
}

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
    } catch (e) { console.error("Błąd obrazka: " + e.message); }
  }
  if (data.pdf && data.pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(
        Utilities.base64Decode(data.pdf.base64),
        "application/pdf",
        data.pdf.filename || "dokument.pdf"
      ));
    } catch (e) { console.error("Błąd PDF: " + e.message); }
  }

  var htmlBody = data.reply_html || "<p>(Brak treści)</p>";
  try {
    msg.reply("", {
      htmlBody:     htmlBody,
      inlineImages: inlineImages,
      attachments:  attachments,
      name:         senderName
    });
    console.log("Wysłano: " + senderName + " -> " + recipient);
  } catch (e) {
    MailApp.sendEmail({
      to:           recipient,
      subject:      "RE: " + subject,
      htmlBody:     htmlBody,
      inlineImages: inlineImages,
      attachments:  attachments,
      name:         senderName
    });
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
    (inlineImages["scrabble_cid"]
      ? '<p><img src="cid:scrabble_cid" alt="Scrabble" style="max-width:100%;"></p>'
      : "");
  try {
    msg.reply("", {
      htmlBody:     htmlBody,
      inlineImages: inlineImages,
      attachments:  attachments,
      name:         "Scrabble – Autoresponder"
    });
  } catch (e) {
    MailApp.sendEmail({
      to:           recipient,
      subject:      "RE: " + subject,
      htmlBody:     htmlBody,
      inlineImages: inlineImages,
      attachments:  attachments,
      name:         "Scrabble – Autoresponder"
    });
  }
}
