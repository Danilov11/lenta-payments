/**
 * Apps Script: выгрузка АПД («Приведи друга») из Google Sheets в личный кабинет.
 *
 * Таблица (строка 1 — заголовки, данные со 2-й):
 *   A: ФИО рекрутера        -> referrer_name (сопоставляется с сотрудником по ФИО)
 *   B: ФИО кого привёл      -> referred_name
 *   C: Телефон кто пришёл   -> referred_phone (по нему считается прогресс по часам)
 *   D: Сумма                -> amount
 *
 * Установка:
 *   1. Открыть нужную Google-таблицу → Расширения → Apps Script.
 *   2. Вставить этот код, сохранить.
 *   3. Указать SHEET_NAME (имя листа) ниже, если лист называется не «Лист1».
 *   4. Запустить функцию syncReferrals один раз (выдать разрешения).
 *   5. Триггеры (значок часов) → добавить триггер: syncReferrals, «По времени»,
 *      ежедневно — для автоматического обновления.
 */

// ── Настройки ───────────────────────────────────────────────────────────────
var API_URL    = 'https://web-production-5bed4.up.railway.app/admin/sync/referrals';
var ADMIN_KEY  = 'lenta-admin-2026';   // должен совпадать с ADMIN_KEY на сервере
var SHEET_NAME = 'Лист1';              // имя листа с данными АПД
// ─────────────────────────────────────────────────────────────────────────────

function syncReferrals() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME)
           || SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

  var values = sheet.getDataRange().getValues();
  var referrals = [];

  // Пропускаем строку заголовков (i = 1)
  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    var referrerName = (row[0] || '').toString().trim();   // A
    var referredName = (row[1] || '').toString().trim();   // B
    var referredPhone = (row[2] || '').toString().trim();  // C
    var amount = row[3];                                    // D

    // Пропускаем пустые строки
    if (!referredName && !referredPhone) continue;

    referrals.push({
      referrer_name:  referrerName,
      referred_name:  referredName,
      referred_phone: referredPhone,
      amount:         amount || 0
    });
  }

  var payload = JSON.stringify({ referrals: referrals });

  var response = UrlFetchApp.fetch(API_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Admin-Key': ADMIN_KEY },
    payload: payload,
    muteHttpExceptions: true
  });

  var code = response.getResponseCode();
  var text = response.getContentText();
  Logger.log('HTTP ' + code + ' — ' + text);

  if (code !== 200) {
    throw new Error('Ошибка выгрузки (' + code + '): ' + text);
  }
  return text;
}
