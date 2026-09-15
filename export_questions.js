// ============================================================
//  三套模拟训练题 导出脚本
//  用法：
//   1. 用已登录的浏览器打开 https://js.zhixinst.com/exam/exam?batchid=... 任意一套
//      （先打开能让考试会话正常建立，避免接口取不到题）
//   2. F12 -> Console -> 粘贴本文件全部内容 -> 回车
//   3. 会自动下载 exam_questions.json，把它发给我即可
//
//  只调用只读接口 GetDetail / GetBatchQuestion，
//  不会调用 BatchStart / BatchDirectStart，不会消耗你的答题次数。
//  密码不出浏览器，token 也不会发给我。
// ============================================================
(async () => {
  const BATCHES = [
    '53bbf0c2-e4e8-42a8-bec1-c93030da385f',
    '762bae22-eed2-4788-a6b1-0df54e3afeec',
    '332f12c0-9b22-4513-a0e0-fad7e3d26119',
  ];
  const BASE = 'https://jsgateway.zhixinst.com';

  const TOKEN =
    localStorage.getItem('ZSJX_WEB_TOKEN') ||
    (() => {
      const k = Object.keys(localStorage).find((x) => /TOKEN/i.test(x));
      return k ? localStorage.getItem(k) : null;
    })();

  if (!TOKEN) {
    console.error('[x] 没找到 token，请确认已登录后再运行');
    return;
  }
  console.log('[ok] token 已找到，长度', TOKEN.length);

  const get = async (path, params) => {
    const qs = new URLSearchParams({
      ...params,
      timestamp: Date.now(),
      batchid: params.batchid,
    });
    const r = await fetch(`${BASE}${path}?${qs}`, {
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const text = await r.text();
    try {
      return JSON.parse(text);
    } catch {
      return { _status: r.status, _raw: text.slice(0, 500) };
    }
  };

  const out = { exportedAt: new Date().toISOString(), batches: {} };

  for (const bid of BATCHES) {
    console.log('拉取中:', bid);
    const rec = {};
    try {
      rec.detail = await get('/ZSJS/WebBatch/GetDetail', { batchid: bid });
      rec.questions = await get('/ZSJS/WebBatch/GetBatchQuestion', { batchid: bid });
      rec.random = await get('/ZSJS/WebBatch/GetBatchRandom', { batchid: bid });
    } catch (e) {
      rec.error = String(e);
    }
    const q = rec.questions;
    const n =
      (q && q.data && (q.data.length || (q.data.list && q.data.list.length))) || 0;
    console.log('  ->', q && q.code, '题目数:', n);
    out.batches[bid] = rec;
  }

  const blob = new Blob([JSON.stringify(out, null, 2)], {
    type: 'application/json',
  });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'exam_questions.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  console.log('[done] 已下载 exam_questions.json，把它发给我');
})();
