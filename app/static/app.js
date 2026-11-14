function toast(msg, kind) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (kind === 'err' ? ' err' : '');
  clearTimeout(window._toastT);
  window._toastT = setTimeout(() => { el.className = 'toast'; }, 3200);
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || ('请求失败 ' + r.status));
  return data;
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const id = btn.dataset.id;
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  btn.disabled = true;
  try {
    if (action === 'requeue') {
      await postJSON(`/api/tasks/${id}/requeue`, {});
      toast('已重新排队 #' + id);
    } else if (action === 'skip') {
      const reason = prompt('跳过原因（会记入审计）：', '站点显示已参保，人工确认跳过');
      if (reason === null) { btn.disabled = false; return; }
      await postJSON(`/api/tasks/${id}/skip`, { reason });
      toast('已跳过 #' + id);
    } else if (action === 'cancel') {
      await postJSON(`/api/tasks/${id}/cancel`, {});
      toast('已取消 #' + id);
    } else if (action === 'run') {
      await postJSON(`/api/tasks/${id}/run`, {});
      toast('已触发执行 #' + id);
    } else if (action === 'fix') {
      const form = btn.closest('.fix-form');
      const changes = {};
      form.querySelectorAll('input,select').forEach(inp => {
        if (inp.name && inp.value !== '' && inp.dataset.orig !== inp.value) changes[inp.name] = inp.value;
      });
      if (!Object.keys(changes).length) { toast('没有修改任何字段', 'err'); btn.disabled = false; return; }
      const res = await postJSON(`/api/tasks/${id}/fix`, { changes });
      toast(res.fingerprint_changed ? '已按新业务指纹建单 #' + res.task_id : '已保存修正并重新排队');
    } else if (action === 'rate-mode') {
      const data = await postJSON('/api/rate-mode', { mode: btn.dataset.mode });
      toast('调度模式：' + data.label);
    } else if (action === 'engine-start' || action === 'engine-stop') {
      const data = await postJSON('/api/engine/' + (action === 'engine-start' ? 'start' : 'stop'), {});
      toast(data.message || 'ok');
    } else if (action === 'batch-run') {
      const data = await postJSON('/api/batches/' + id + '/run', {});
      toast('批次已排队：' + data.queued + ' 个任务');
    }
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.disabled = false;
    setTimeout(() => {
      if (!location.pathname.startsWith('/receipts') || btn.dataset.reload !== '0') location.reload();
    }, 450);
  }
});

// 文本框原始值记录，用于计算 diff
document.querySelectorAll('.fix-form input, .fix-form select').forEach(inp => {
  inp.dataset.orig = inp.value;
});

// 队列页自动刷新
if (document.body.dataset.autorefresh === '1') {
  setTimeout(() => location.reload(), 3500);
}
