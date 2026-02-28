/**
 * processEmailsFinal - Google Apps Script
 * Script Properties:
 *   WEBHOOK_URL - URL do Twojego backendu (np. Render)
 *   WEBHOOK_SECRET - (opcjonalnie) nagłówek X-Webhook-Secret
 *   BIZ_LIST - przecinek-separated emails (opcjonalnie)
 *   ALLOWED_LIST - przecinek-separated emails (opcjonalnie)
 *   KEYWORDS - przecinek-separated keywords (opcjonalnie)
 */

function _getListFromProps(name) {
  var props = PropertiesService.getScriptProperties();
  var raw = props.getProperty(name) || "";
  return raw.split(",").map(function(s){ return s.trim().toLowerCase(); }).filter(Boolean);
}

function processEmailsFinal() {
  var props = PropertiesService.getScriptProperties();
  var webhookUrl = props.getProperty("WEBHOOK_URL");
  if (!webhookUrl) {
    console.error("Brak WEBHOOK_URL w Script Properties!");
    return;
  }

  var BIZ_LIST = _getListFromProps("BIZ_LIST");
  var ALLOWED_LIST = _getListFromProps("ALLOWED_LIST");
  var KEYWORDS = _getListFromProps("KEYWORDS");

  var threads = GmailApp.getInboxThreads(0, 20);
  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    if (!thread.isUnread()) continue;

    var messages = thread.getMessages();
    var msg = messages[messages.length - 1];
    var fromRaw = msg.getFrom();
    var fromEmail = extractEmail(fromRaw).toLowerCase();

    // decyzja: czy przetwarzać i którą odpowiedź wysłać
    var isBiz = BIZ_LIST.indexOf(fromEmail) !== -1;
    var isAllowed = ALLOWED_LIST.indexOf(fromEmail) !== -1;
    var containsKeyword = KEYWORDS.some(function(k){ return k && msg.getPlainBody().toLowerCase().indexOf(k) !== -1; });

    if (!isBiz && !isAllowed && !containsKeyword) {
      // ignorujemy
      thread.addLabel(GmailApp.getUserLabelByName("processed"));
      continue;
    }

    // Wywołanie backendu
    var response = _callBackend(fromEmail, msg.getSubject(), msg.getPlainBody(), webhookUrl);
    if (response && response.json) {
      // Backend zwraca obie sekcje; Script decyduje co wysłać:
      var json = response.json;

      // Jeśli nadawca jest na BIZ_LIST -> wysyłamy tylko biznesową odpowiedź
      if (isBiz && json.biznes) {
        executeMailSend(json.biznes, fromEmail, msg.getSubject(), msg, "Notariusz – Informacja");
      }

      // Jeśli nadawca jest na ALLOWED_LIST -> wysyłamy tylko emocjonalną odpowiedź
      if (isAllowed && json.zwykly) {
        executeMailSend(json.zwykly, fromEmail, msg.getSubject(), msg, "Tyler Durden – Autoresponder");
      }

      // Jeśli nadawca nie jest na żadnej liście, ale zawiera keyword -> wysyłamy obie odpowiedzi (biz + zwykly)
      if (!isBiz && !isAllowed && containsKeyword) {
        if (json.biznes) {
          executeMailSend(json.biznes, fromEmail, msg.getSubject(), msg, "Notariusz – Informacja");          
        }
        if (json.zwykly) {
          executeMailSend(json.zwykly, fromEmail, msg.getSubject(), msg, "Tyler Durden – Autoresponder");
        }
      }

      
    }

    thread.markRead();
  }
}

function extractEmail(fromHeader) {
  var m = fromHeader.match(/<([^>]+)>/);
  if (m) return m[1];
  return fromHeader.split(" ")[0];
}

function _callBackend(sender, subject, body, url) {
  var secret = PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET");
  var options = {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({
      from: sender,
      subject: subject,
      body: body
    }),
    muteHttpExceptions: true,
    headers: secret ? { "X-Webhook-Secret": secret } : {}
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
  var attachments = [];

  // inline image (base64)
  if (data.emoticon && data.emoticon.base64) {
    try {
      var imgBlob = Utilities.newBlob(Utilities.base64Decode(data.emoticon.base64), data.emoticon.content_type || "image/png", data.emoticon.filename || "emotka.png");
      inlineImages["emotka_cid"] = imgBlob;
    } catch (e) {
      console.error("Błąd dekodowania obrazka: " + e.message);
    }
  }

  // pdf attachment
  if (data.pdf && data.pdf.base64) {
    try {
      attachments.push(Utilities.newBlob(Utilities.base64Decode(data.pdf.base64), "application/pdf", data.pdf.filename || "dokument.pdf"));
    } catch (e) {
      console.error("Błąd dekodowania PDF: " + e.message);
    }
  }

  var htmlBody = data.reply_html || "<p>(Brak treści)</p>";
  // jeśli inlineImages zawiera emotka_cid, upewnij się, że w HTML jest <img src="cid:emotka_cid">
  try {
    msg.reply("", {
      htmlBody: htmlBody,
      inlineImages: inlineImages,
      attachments: attachments,
      name: senderName
    });
    console.log("Wysłano odpowiedź: " + senderName + " -> " + recipient);
  } catch (e) {
    console.warn("reply() nie działa, wysyłam nowy mail. Powód: " + e.message);
    MailApp.sendEmail({
      to: recipient,
      subject: "RE: " + subject,
      htmlBody: htmlBody,
      inlineImages: inlineImages,
      attachments: attachments,
      name: senderName
    });
  }
}
