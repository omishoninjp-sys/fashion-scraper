/**
 * Logger — 記憶體日誌 + console 輸出
 * Zeabur 上可從 dashboard 看 console，也可透過 /logs API 查看
 */

const MAX_LOGS = 500;
const logs = [];

function log(message) {
  const timestamp = new Date().toISOString();
  const line = `[${timestamp}] ${message}`;
  console.log(line);
  logs.push(line);
  if (logs.length > MAX_LOGS) logs.shift();
}

function getLogs(count = 100) {
  return logs.slice(-count);
}

module.exports = { log, getLogs };
