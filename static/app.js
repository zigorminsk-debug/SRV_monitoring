(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {
    overview: null,
    history: [],
    alerts: [],
    settings: { poll_interval_seconds: 60 },
    assetFilter: 'all',
    currentView: 'dashboard',
    loading: false,
    editingSource: null,
  };

  const TYPE_LABELS = {
    vmware: 'VMware ESXi / vCenter',
    redfish: 'Аппаратный мониторинг · Redfish',
    ipmi: 'Аппаратный мониторинг · IPMI',
    backup_json: 'Резервное копирование · JSON API',
    agent: 'Удалённый Windows-агент',
  };
  const TYPE_MARKS = { vmware: 'V', redfish: 'R', ipmi: 'I', backup_json: 'B', agent: 'W' };
  const STATUS_LABELS = { ok: 'В норме', warning: 'Проверить', critical: 'Критично', unknown: 'Нет данных' };
  const TYPE_HINTS = {
    vmware: 'Читайте состояние хостов, аппаратные сенсоры, физические SCSI-диски и свободное место datastore через vSphere API.',
    redfish: 'Стандартный DMTF Redfish API для BMC (iDRAC, iLO, XClarity и совместимые контроллеры).',
    ipmi: 'Сбор аппаратных датчиков и System Event Log через ipmitool. Утилита должна быть установлена на сервере мониторинга.',
    backup_json: 'Читает JSON API только на чтение. Укажите endpoint и JSON-пути статусов, соответствующие версии вашей системы резервного копирования.',
  };

  async function api(path, options = {}) {
    const init = { credentials: 'same-origin', ...options };
    init.headers = { ...(options.headers || {}) };
    if (options.body && !init.headers['Content-Type']) init.headers['Content-Type'] = 'application/json';
    const response = await fetch(path, init);
    let payload = {};
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      try { payload = await response.json(); } catch (_) { payload = {}; }
    } else if (!response.ok) {
      payload = { detail: await response.text() };
    }
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      showLogin();
      throw new Error('Сессия завершена. Войдите снова.');
    }
    if (!response.ok) {
      const detail = payload.detail || payload.message || `Ошибка HTTP ${response.status}`;
      const message = Array.isArray(detail) ? detail.map((part) => part.msg || part.message || '').filter(Boolean).join('. ') : String(detail);
      throw new Error(message || `Ошибка HTTP ${response.status}`);
    }
    return payload;
  }

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
  }

  function showToast(message, kind = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${kind}`;
    toast.textContent = message;
    $('#toastRegion').append(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  function showLogin(error = '') {
    $('#appView').classList.add('hidden');
    $('#loginView').classList.remove('hidden');
    $('#loginError').textContent = error;
    $('#loginError').classList.toggle('hidden', !error);
    $('#loginPassword').value = '';
  }

  function showApp(username = 'admin') {
    $('#loginView').classList.add('hidden');
    $('#appView').classList.remove('hidden');
    $('#profileName').textContent = username;
    $('#loginError').classList.add('hidden');
    $('#todayLabel').textContent = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(new Date()).toUpperCase();
  }

  function formatDate(value, withDate = false) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    const options = withDate
      ? { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' };
    return new Intl.DateTimeFormat('ru-RU', options).format(date).replace(',', '');
  }

  function relativeTime(value) {
    if (!value) return 'нет данных';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'нет данных';
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return `${seconds} сек. назад`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} мин. назад`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч. назад`;
    return `${Math.floor(seconds / 86400)} дн. назад`;
  }

  function statusPill(status) {
    const safe = ['ok', 'warning', 'critical', 'unknown'].includes(status) ? status : 'unknown';
    return `<span class="status-pill ${safe}">${STATUS_LABELS[safe]}</span>`;
  }

  function shortNumber(value, suffix = '') {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '—';
    const num = Number(value);
    return `${num % 1 ? num.toFixed(1) : num}${suffix}`;
  }

  function metricBar(label, value, inverse = false) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
    const num = Math.max(0, Math.min(100, Number(value)));
    const shown = inverse ? `${num.toFixed(0)}% free` : `${num.toFixed(0)}%`;
    const fill = inverse ? 100 - num : num;
    const low = fill >= 90 || (inverse && num <= 10) ? ' low' : '';
    return `<div class="metric-cell"><span>${escapeHTML(label)} ${shown}</span><div class="mini-progress"><i class="${label === 'RAM' ? 'mem' : ''}${low}" style="width:${fill}%"></i></div></div>`;
  }

  function deviceMetrics(device) {
    const metrics = device.metrics || {};
    const pieces = [];
    const cpu = metrics.cpu_percent;
    const memory = metrics.memory_percent;
    const diskFree = metrics.disk_min_free_percent ?? metrics.datastore_min_free_percent;
    if (cpu !== null && cpu !== undefined) pieces.push(metricBar('CPU', cpu));
    if (memory !== null && memory !== undefined) pieces.push(metricBar('RAM', memory));
    if (diskFree !== null && diskFree !== undefined) pieces.push(metricBar('DISK', diskFree, true));
    if (!pieces.length && metrics.temperature_max_c !== null && metrics.temperature_max_c !== undefined) {
      pieces.push(`<div class="metric-cell"><span>TEMP ${escapeHTML(shortNumber(metrics.temperature_max_c, '°'))}C</span></div>`);
    }
    return pieces.join('') || '<span class="muted">—</span>';
  }

  function deviceIcon(device) {
    if (device.entity_type === 'agent' || device.type === 'agent') return 'agent';
    if (device.type === 'backup_json') return 'backup';
    return '';
  }

  function renderOverview() {
    const data = state.overview;
    if (!data) return;
    const counts = data.counts || { total: 0, critical: 0, warning: 0, unknown: 0, ok: 0 };
    const devices = data.devices || [];
    const alerts = data.alerts || [];
    const critical = counts.critical || 0;
    const warning = counts.warning || 0;
    const unknown = counts.unknown || 0;
    const ok = counts.ok || 0;
    const healthyPercent = counts.total ? Math.round((ok / counts.total) * 100) : 0;
    $('#heroDeviceTotal').textContent = counts.total || 0;
    $('#healthPercent').textContent = counts.total ? `${healthyPercent}%` : '—';
    $('#healthRing').style.background = `conic-gradient(#56d1b0 ${healthyPercent * 3.6}deg, rgba(255,255,255,.14) ${healthyPercent * 3.6}deg)`;
    $('#heroInterval').textContent = formatInterval(data.poll_interval_seconds);
    $('#kpiDevices').textContent = counts.total || 0;
    $('#kpiDevicesSub').textContent = `${ok} в норме`;
    $('#kpiCritical').textContent = critical;
    $('#kpiWarning').textContent = warning + unknown;
    $('#kpiDevicesFoot').textContent = `${data.sources.length} источников · ${data.agents.length} удалённых агентов`;
    $('#navDeviceCount').textContent = counts.total || 0;
    $('#navAlertCount').textContent = alerts.length;
    $('#navAlertCount').classList.toggle('hidden', alerts.length === 0);
    $('#sidebarPollInterval').textContent = formatInterval(data.poll_interval_seconds);
    $('#sidebarLastSync').textContent = relativeTime(latestCollectedTime(devices));
    $('#topLastSync').textContent = latestCollectedTime(devices) ? `Данные ${relativeTime(latestCollectedTime(devices))}` : 'Ожидание данных';

    if (critical) {
      $('#heroTitle').textContent = 'Обнаружены критические проблемы';
      $('#heroSubtitle').textContent = `${critical} устройств требуют немедленного внимания. Откройте журнал событий для подробностей.`;
    } else if (warning) {
      $('#heroTitle').textContent = 'Есть вопросы для проверки';
      $('#heroSubtitle').textContent = `${warning} устройств в статусе предупреждения. Работа мониторинга продолжается.`;
    } else if (unknown || !counts.total) {
      $('#heroTitle').textContent = counts.total ? 'Ожидаем данные от узлов' : 'Добавьте источники мониторинга';
      $('#heroSubtitle').textContent = counts.total ? 'Некоторые устройства ещё не отправили первый heartbeat.' : 'Подключите vCenter, BMC, систему бэкапов или удалённые Windows-узлы.';
    } else {
      $('#heroTitle').textContent = 'Все системы работают штатно';
      $('#heroSubtitle').textContent = 'Критических проблем не обнаружено. Мониторинг инфраструктуры активен.';
    }

    renderBackupKpi(devices);
    renderRecentAlerts(alerts);
    renderDeviceTable(devices, $('#deviceSearch').value || '');
    renderAssets();
    renderManagementLists();
    renderAlertPage();
    renderChart();
    $('#deviceTableCount').textContent = `${counts.total} ${plural(counts.total, 'устройство', 'устройства', 'устройств')}`;
    $('#assetAllCount').textContent = counts.total;
    $('#assetSourceCount').textContent = data.sources.length;
    $('#assetAgentCount').textContent = data.agents.length;
    $('#settingsSourceCount').textContent = data.sources.length;
    $('#settingsAgentCount').textContent = data.agents.length;
    $('#appVersion').textContent = `v${data.version || '0.1.0'}`;
    if (document.activeElement !== $('#pollInterval')) $('#pollInterval').value = data.poll_interval_seconds;
  }

  function latestCollectedTime(devices) {
    const dates = devices.map((device) => new Date(device.last_seen || 0).getTime()).filter((time) => time > 0);
    return dates.length ? new Date(Math.max(...dates)).toISOString() : null;
  }

  function formatInterval(seconds) {
    const value = Number(seconds) || 60;
    if (value < 60) return `${value} сек.`;
    if (value % 3600 === 0) return `${value / 3600} ч.`;
    if (value % 60 === 0) return `${value / 60} мин.`;
    return `${Math.floor(value / 60)} мин.`;
  }

  function plural(number, one, few, many) {
    const n = Math.abs(number) % 100;
    const last = n % 10;
    if (n > 10 && n < 20) return many;
    if (last > 1 && last < 5) return few;
    if (last === 1) return one;
    return many;
  }

  function renderBackupKpi(devices) {
    const backup = devices.filter((device) => device.type === 'backup_json' || (device.entity_type === 'agent' && (device.metrics?.backup_job_count !== undefined || (device.checks || []).some((check) => /vinchin|backup|бэкап/i.test(check.name || '')))));
    if (!backup.length) {
      $('#kpiBackup').textContent = '—';
      $('#kpiBackupTime').textContent = 'не подключено';
      $('#kpiBackupFoot').textContent = 'Подключите JSON API Vinchin';
      return;
    }
    const failed = backup.reduce((total, device) => total + Number(device.metrics?.backup_failed_count || 0), 0);
    const successful = backup.reduce((total, device) => total + Number(device.metrics?.backup_succeeded_count || 0), 0);
    const status = backup.some((device) => device.status === 'critical') ? 'Ошибка' : backup.some((device) => device.status === 'warning') ? 'Проверить' : failed ? 'Ошибка' : (successful || backup.some((device) => device.status === 'ok')) ? 'Успешно' : 'Ожидание';
    $('#kpiBackup').textContent = status;
    $('#kpiBackupTime').textContent = relativeTime(backup.map((item) => item.last_seen).filter(Boolean).sort().at(-1));
    $('#kpiBackupFoot').textContent = `${backup.length} ${plural(backup.length, 'система', 'системы', 'систем')} · ${failed} ошибок заданий`;
  }

  function renderRecentAlerts(alerts) {
    const target = $('#recentAlerts');
    if (!alerts.length) {
      target.innerHTML = '<div class="empty-state compact"><span class="empty-icon">✓</span><b>Событий пока нет</b><small>Новые тревоги появятся здесь автоматически.</small></div>';
      $('#activeAlertsText').textContent = 'Нет активных событий';
      return;
    }
    target.innerHTML = alerts.slice(0, 4).map((alert) => `
      <div class="recent-alert-row">
        <span class="event-dot ${escapeHTML(alert.severity)}"></span>
        <div class="event-copy"><b>${escapeHTML(alert.subject)}</b><small>${escapeHTML(alert.message)}</small></div>
        <span class="event-time">${escapeHTML(relativeTime(alert.last_seen))}</span>
      </div>`).join('');
    $('#activeAlertsText').textContent = `${alerts.length} активных ${plural(alerts.length, 'событие', 'события', 'событий')}`;
  }

  function renderDeviceTable(devices, filter = '') {
    const target = $('#deviceTableBody');
    const needle = filter.trim().toLowerCase();
    const filtered = devices.filter((device) => `${device.name} ${device.label || ''} ${TYPE_LABELS[device.type] || ''} ${device.summary || ''}`.toLowerCase().includes(needle));
    if (!filtered.length) {
      target.innerHTML = `<tr><td colspan="6"><div class="empty-state table-empty"><span class="empty-icon">⌕</span><b>${devices.length ? 'Ничего не найдено' : 'Источники мониторинга не добавлены'}</b><small>${devices.length ? 'Измените поисковый запрос.' : 'Добавьте ESXi, Redfish/IPMI, Backup API или подключите удалённый агент.'}</small></div></td></tr>`;
      return;
    }
    target.innerHTML = filtered.slice(0, 12).map((device) => {
      const label = TYPE_LABELS[device.type] || TYPE_LABELS.agent;
      const icon = deviceIcon(device);
      return `<tr>
        <td><div class="device-main"><span class="device-avatar ${icon}">${escapeHTML(TYPE_MARKS[device.type] || 'S')}</span><span class="device-name"><b>${escapeHTML(device.name)}</b><small>${escapeHTML(device.label || device.summary || device.name)}</small></span></div></td>
        <td><span class="type-label"><span class="type-mark">${escapeHTML(TYPE_MARKS[device.type] || 'S')}</span>${escapeHTML(label)}</span></td>
        <td>${statusPill(device.status)}</td>
        <td>${escapeHTML(relativeTime(device.last_seen))}</td>
        <td><div class="metrics-inline">${deviceMetrics(device)}</div></td>
        <td><button class="row-action" data-device-detail="${escapeHTML(device.entity_type)}:${escapeHTML(device.id)}" title="Подробнее">···</button></td>
      </tr>`;
    }).join('');
  }

  function renderAssets() {
    const target = $('#assetCards');
    const devices = state.overview?.devices || [];
    const needle = ($('#assetSearch')?.value || '').trim().toLowerCase();
    const filtered = devices.filter((device) => {
      const typeMatch = state.assetFilter === 'all' || (state.assetFilter === 'agent' ? device.entity_type === 'agent' : device.entity_type === 'source');
      const textMatch = `${device.name} ${device.label || ''} ${TYPE_LABELS[device.type] || ''} ${device.summary || ''}`.toLowerCase().includes(needle);
      return typeMatch && textMatch;
    });
    if (!filtered.length) {
      target.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><span class="empty-icon">⌘</span><b>${devices.length ? 'Устройств не найдено' : 'Добавьте первое устройство'}</b><small>${devices.length ? 'Попробуйте изменить фильтр или поиск.' : 'Создайте источник ESXi, аппаратного BMC или системы резервного копирования.'}</small></div>`;
      return;
    }
    target.innerHTML = filtered.map((device) => {
      const type = TYPE_LABELS[device.type] || TYPE_LABELS.agent;
      const icon = deviceIcon(device);
      const m = device.metrics || {};
      const measurements = [
        ['CPU', m.cpu_percent], ['RAM', m.memory_percent],
        ['FREE', m.disk_min_free_percent ?? m.datastore_min_free_percent],
        ['TEMP', m.temperature_max_c],
      ].filter(([, value]) => value !== null && value !== undefined).slice(0, 3);
      return `<article class="asset-card">
        <div class="asset-card-head"><span class="device-avatar ${icon}">${escapeHTML(TYPE_MARKS[device.type] || 'S')}</span><div class="asset-card-ident"><b>${escapeHTML(device.name)}</b><small>${escapeHTML(type)}</small></div>${statusPill(device.status)}</div>
        <div class="asset-card-body">${escapeHTML(device.summary || 'Нет дополнительной информации')}${device.error ? `<br><span class="error-inline">${escapeHTML(device.error)}</span>` : ''}</div>
        <div class="asset-card-metrics">${measurements.length ? measurements.map(([label, value]) => `<div class="asset-metric"><span>${escapeHTML(label)}</span><b>${escapeHTML(shortNumber(value, label === 'TEMP' ? '°C' : '%'))}</b></div>`).join('') : '<div class="asset-metric"><span>ИЗМЕРЕНИЯ</span><b>—</b></div>'}</div>
        <div class="asset-card-foot"><span>Обновлено ${escapeHTML(relativeTime(device.last_seen))}</span><div class="asset-card-actions"><button data-device-detail="${escapeHTML(device.entity_type)}:${escapeHTML(device.id)}">Подробности</button>${device.entity_type === 'source' ? `<button data-source-edit="${escapeHTML(device.id)}">Настроить</button>` : ''}</div></div>
      </article>`;
    }).join('');
  }

  function renderManagementLists() {
    const sources = state.overview?.sources || [];
    const agents = state.overview?.agents || [];
    const sourceTarget = $('#sourceManagementList');
    if (!sources.length) {
      sourceTarget.innerHTML = '<div class="empty-state compact"><b>Источников пока нет</b><small>Добавьте устройства, которые требуется контролировать.</small></div>';
    } else {
      sourceTarget.innerHTML = sources.map((source) => {
        const address = source.config.host || source.config.url || 'Параметры подключения заданы';
        return `<div class="source-management-row"><div class="management-name"><span class="device-avatar ${source.type === 'backup_json' ? 'backup' : ''}">${escapeHTML(TYPE_MARKS[source.type] || 'S')}</span><div><b>${escapeHTML(source.name)}</b><small>${escapeHTML(TYPE_LABELS[source.type] || source.type)}</small></div></div><span class="management-detail">${escapeHTML(address)}</span><span>${statusPill(source.last_status)}</span><div class="management-actions"><button data-source-poll="${escapeHTML(source.id)}">Опросить</button><button data-source-edit="${escapeHTML(source.id)}">Изменить</button><button class="danger" data-source-delete="${escapeHTML(source.id)}">Удалить</button></div></div>`;
      }).join('');
    }
    const agentTarget = $('#agentManagementList');
    if (!agents.length) {
      agentTarget.innerHTML = '<div class="empty-state compact"><b>Агенты ещё не подключены</b><small>Создайте токен и установите агент на удалённый Windows-сервер.</small></div>';
    } else {
      agentTarget.innerHTML = agents.map((agent) => {
        const latest = agent.latest || {};
        const hostname = latest.hostname || agent.name;
        return `<div class="source-management-row"><div class="management-name"><span class="device-avatar agent">W</span><div><b>${escapeHTML(agent.name)}</b><small>${escapeHTML(hostname)}</small></div></div><span class="management-detail">Последний heartbeat: ${escapeHTML(relativeTime(agent.last_seen))}</span><span>${statusPill(agent.last_status)}</span><div class="management-actions"><button class="danger" data-agent-revoke="${escapeHTML(agent.id)}">Отключить</button></div></div>`;
      }).join('');
    }
  }

  function renderAlertPage() {
    const alerts = state.alerts || [];
    const activeCritical = alerts.filter((alert) => !alert.resolved_at && alert.severity === 'critical').length;
    const activeWarning = alerts.filter((alert) => !alert.resolved_at && alert.severity === 'warning').length;
    const resolved = alerts.filter((alert) => !!alert.resolved_at).length;
    $('#alertCriticalCount').textContent = activeCritical;
    $('#alertWarningCount').textContent = activeWarning;
    $('#alertResolvedCount').textContent = resolved;
    const filter = $('#alertFilter')?.value || 'active';
    const visible = alerts.filter((alert) => {
      if (filter === 'active' && alert.resolved_at) return false;
      if (filter === 'critical' && alert.severity !== 'critical') return false;
      if (filter === 'warning' && alert.severity !== 'warning') return false;
      return true;
    });
    $('#alertFeedCount').textContent = `${visible.length} записей`;
    const target = $('#fullAlerts');
    if (!visible.length) {
      target.innerHTML = '<div class="empty-state"><span class="empty-icon">✓</span><b>Все системы работают штатно</b><small>Когда мониторинг обнаружит проблему, она появится в этом журнале.</small></div>';
      return;
    }
    target.innerHTML = visible.map((alert) => {
      const resolvedState = !!alert.resolved_at;
      const severity = resolvedState ? 'resolved' : alert.severity;
      return `<div class="full-alert-row"><span class="event-dot ${escapeHTML(severity)}"></span><div class="full-alert-main"><b>${escapeHTML(alert.subject)}</b><small>${escapeHTML(alert.message)}${resolvedState ? ` · Разрешено ${escapeHTML(relativeTime(alert.resolved_at))}` : ''}</small></div><span class="full-alert-time">${escapeHTML(relativeTime(alert.last_seen))}</span>${resolvedState ? '<span class="status-pill ok">Разрешено</span>' : `<button class="ack-button ${alert.acknowledged_at ? 'acked' : ''}" data-alert-ack="${escapeHTML(alert.id)}" data-acknowledged="${alert.acknowledged_at ? '1' : '0'}">${alert.acknowledged_at ? '✓ Принято' : 'Принять'}</button>`}</div>`;
    }).join('');
  }

  function renderChart() {
    const hours = Number($('#trendRange')?.value || 24);
    const timeLabels = $$('.chart-x-labels span');
    timeLabels.forEach((label, index) => {
      label.textContent = index === timeLabels.length - 1 ? 'Сейчас' : `−${Math.round(hours * (1 - index / (timeLabels.length - 1)))}ч`;
    });
    const cutoff = Date.now() - hours * 3600 * 1000;
    const rows = (state.history || []).filter((row) => new Date(row.collected_at).getTime() >= cutoff);
    const cpuSamples = rows.map((row) => ({ time: new Date(row.collected_at).getTime(), value: Number(row.metrics?.cpu_percent) })).filter((item) => Number.isFinite(item.time) && Number.isFinite(item.value));
    const memorySamples = rows.map((row) => ({ time: new Date(row.collected_at).getTime(), value: Number(row.metrics?.memory_percent) })).filter((item) => Number.isFinite(item.time) && Number.isFinite(item.value));
    const paths = { cpu: $('.cpu-line'), memory: $('.memory-line'), cpuFill: $('.cpu-fill'), memoryFill: $('.memory-fill') };
    if (!cpuSamples.length && !memorySamples.length) {
      Object.values(paths).forEach((path) => path.setAttribute('d', ''));
      $('#chartPoints').innerHTML = '';
      $('#chartEmpty').classList.remove('hidden');
      $('#chartCpuAverage').textContent = '—';
      $('#chartMemoryAverage').textContent = '—';
      return;
    }
    $('#chartEmpty').classList.add('hidden');
    const buckets = 16;
    const start = cutoff;
    const now = Date.now();
    function averageSeries(samples) {
      const values = Array.from({ length: buckets }, () => []);
      for (const sample of samples) {
        const index = Math.max(0, Math.min(buckets - 1, Math.floor(((sample.time - start) / Math.max(1, now - start)) * buckets)));
        values[index].push(Math.max(0, Math.min(100, sample.value)));
      }
      const series = [];
      let last = null;
      for (let i = 0; i < buckets; i += 1) {
        if (values[i].length) last = values[i].reduce((sum, n) => sum + n, 0) / values[i].length;
        series.push(last);
      }
      const first = series.findIndex((value) => value !== null);
      if (first > 0) for (let i = 0; i < first; i += 1) series[i] = series[first];
      return series;
    }
    const cpu = averageSeries(cpuSamples);
    const memory = averageSeries(memorySamples);
    const y = (value) => 178 - (Math.max(0, Math.min(100, value)) / 100) * 168;
    function makePath(series) {
      const points = series.map((value, index) => ({ x: (index / (buckets - 1)) * 720, y: value === null ? null : y(value) })).filter((point) => point.y !== null);
      if (!points.length) return { line: '', fill: '' };
      if (points.length === 1) points.push({ x: 720, y: points[0].y });
      const line = points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
      const fill = `${line} L${points[points.length - 1].x.toFixed(1)},178 L${points[0].x.toFixed(1)},178 Z`;
      return { line, fill };
    }
    const cpuPath = makePath(cpu);
    const memoryPath = makePath(memory);
    paths.cpu.setAttribute('d', cpuPath.line);
    paths.cpuFill.setAttribute('d', cpuPath.fill);
    paths.memory.setAttribute('d', memoryPath.line);
    paths.memoryFill.setAttribute('d', memoryPath.fill);
    $('#chartCpuAverage').textContent = cpuSamples.length ? `${Math.round(cpuSamples.reduce((sum, item) => sum + item.value, 0) / cpuSamples.length)}%` : '—';
    $('#chartMemoryAverage').textContent = memorySamples.length ? `${Math.round(memorySamples.reduce((sum, item) => sum + item.value, 0) / memorySamples.length)}%` : '—';
    $('#chartPoints').innerHTML = '';
  }

  async function loadData() {
    if (state.loading || $('#appView').classList.contains('hidden')) return;
    state.loading = true;
    $('#refreshButton').classList.add('is-loading');
    try {
      const [overview, history, alerts] = await Promise.all([
        api('/api/overview'),
        api(`/api/history?hours=${encodeURIComponent($('#trendRange').value || 24)}&limit=1000`),
        api('/api/alerts?limit=500'),
      ]);
      state.overview = overview;
      state.history = history;
      state.alerts = alerts;
      state.settings.poll_interval_seconds = overview.poll_interval_seconds;
      renderOverview();
    } catch (error) {
      if (!String(error.message).includes('Сессия завершена')) showToast(error.message, 'error');
    } finally {
      state.loading = false;
      $('#refreshButton').classList.remove('is-loading');
    }
  }

  function showView(view) {
    if (!['dashboard', 'alerts', 'assets', 'settings'].includes(view)) return;
    state.currentView = view;
    $$('.page-view').forEach((element) => element.classList.toggle('active-view', element.id === `view-${view}`));
    $$('.nav-link[data-view]').forEach((element) => element.classList.toggle('active', element.dataset.view === view));
    const titles = { dashboard: 'Обзор', alerts: 'События', assets: 'Устройства', settings: 'Настройки' };
    $('#pageTitle').textContent = titles[view];
    if (view === 'alerts') renderAlertPage();
    if (view === 'assets') renderAssets();
    if (view === 'settings') renderManagementLists();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function openModal(id) {
    $(id).classList.remove('hidden');
    document.body.classList.add('modal-open');
  }

  function closeModal(element) {
    if (element) element.classList.add('hidden');
    if (!$('.modal-backdrop:not(.hidden)')) document.body.classList.remove('modal-open');
  }

  function fieldHTML(label, key, value = '', options = {}) {
    const span = options.span ? ' field-span' : '';
    if (options.checkbox) {
      const checked = value === false ? '' : ' checked';
      return `<label class="checkbox-field${span}"><input type="checkbox" data-config-key="${escapeHTML(key)}"${checked}> ${escapeHTML(label)}</label>`;
    }
    if (options.select) {
      const selected = String(value || options.defaultValue || 'basic');
      const opts = options.select.map(([v, text]) => `<option value="${escapeHTML(v)}" ${selected === v ? 'selected' : ''}>${escapeHTML(text)}</option>`).join('');
      return `<label class="field-label${span}">${escapeHTML(label)}<select class="form-input" data-config-key="${escapeHTML(key)}">${opts}</select></label>`;
    }
    const type = options.type || (options.secret ? 'password' : 'text');
    const placeholder = options.placeholder || (options.secret && options.hasSecret ? 'Сохранён · оставьте пустым, чтобы не менять' : '');
    const inputValue = options.secret ? '' : value;
    const minMax = options.min !== undefined ? ` min="${options.min}"` : '';
    const maxAttr = options.max !== undefined ? ` max="${options.max}"` : '';
    const stepAttr = options.step !== undefined ? ` step="${options.step}"` : '';
    const required = options.required ? ' required' : '';
    const help = options.help ? `<small class="field-help${span}">${escapeHTML(options.help)}</small>` : '';
    return `<label class="field-label${span}" for="cfg-${escapeHTML(key)}">${escapeHTML(label)}<input id="cfg-${escapeHTML(key)}" class="form-input" data-config-key="${escapeHTML(key)}" type="${type}" value="${escapeHTML(inputValue)}" placeholder="${escapeHTML(placeholder)}"${minMax}${maxAttr}${stepAttr}${required}></label>${help}`;
  }

  function renderSourceFields(type, config = {}, source = null) {
    const container = $('#sourceDynamicFields');
    let html = `<div class="field-span type-help">${escapeHTML(TYPE_HINTS[type] || '')}</div>`;
    if (type === 'vmware') {
      html += fieldHTML('Адрес ESXi / vCenter', 'host', config.host || '', { required: true, placeholder: 'vcenter.example.local или IP' });
      html += fieldHTML('Порт API', 'port', config.port ?? 443, { type: 'number', min: 1, max: 65535 });
      html += fieldHTML('Пользователь', 'username', config.username || '', { required: true, placeholder: 'monitoring@vsphere.local' });
      html += fieldHTML('Пароль', 'password', '', { secret: true, required: !source, hasSecret: source?.has_password });
      html += fieldHTML('Предупреждение datastore: свободно ≤ %', 'datastore_warning_percent', config.datastore_warning_percent ?? 15, { type: 'number', min: 1, max: 99, step: 1 });
      html += fieldHTML('Критический datastore: свободно ≤ %', 'datastore_critical_percent', config.datastore_critical_percent ?? 8, { type: 'number', min: 0, max: 99, step: 1 });
      html += fieldHTML('Проверять TLS-сертификат', 'verify_tls', config.verify_tls !== false, { checkbox: true, span: true });
    } else if (type === 'redfish') {
      html += fieldHTML('Базовый URL Redfish', 'url', config.url || '', { required: true, span: true, placeholder: 'https://10.10.0.15' });
      html += fieldHTML('Пользователь BMC', 'username', config.username || '', { placeholder: 'read-only user' });
      html += fieldHTML('Пароль', 'password', '', { secret: true, hasSecret: source?.has_password });
      html += fieldHTML('Проверять TLS-сертификат', 'verify_tls', config.verify_tls !== false, { checkbox: true, span: true });
      html += '<div class="field-span"><small class="field-help">Для промышленной эксплуатации используйте доверенный сертификат BMC. Проверка TLS включена по умолчанию.</small></div>';
    } else if (type === 'ipmi') {
      html += fieldHTML('Адрес BMC / IPMI', 'host', config.host || '', { required: true, placeholder: '10.10.0.15' });
      html += fieldHTML('Путь к ipmitool.exe', 'ipmitool_path', config.ipmitool_path || 'ipmitool', { placeholder: 'C:\\Program Files\\ipmitool\\ipmitool.exe' });
      html += fieldHTML('Пользователь BMC', 'username', config.username || '', { required: true });
      html += fieldHTML('Пароль', 'password', '', { secret: true, required: !source, hasSecret: source?.has_password });
      html += '<div class="field-span"><small class="field-help">На Windows установите ipmitool и укажите полный путь к исполняемому файлу при необходимости.</small></div>';
    } else {
      html += fieldHTML('URL API / JSON endpoint', 'url', config.url || '', { required: true, span: true, placeholder: 'https://backup.example.local/api/jobs' });
      html += fieldHTML('Тип авторизации', 'auth_type', config.auth_type || 'basic', { select: [['basic', 'Basic auth'], ['bearer', 'Bearer token'], ['none', 'Без авторизации']], span: true });
      html += fieldHTML('Пользователь API', 'username', config.username || '', { placeholder: 'API user' });
      html += fieldHTML('Пароль API', 'password', '', { secret: true, hasSecret: source?.has_password });
      html += fieldHTML('Bearer-токен', 'bearer_token', '', { secret: true, hasSecret: source?.has_bearer_token });
      html += fieldHTML('JSON-путь к списку заданий', 'jobs_path', config.jobs_path ?? 'jobs', { placeholder: 'jobs или data.jobs' });
      html += fieldHTML('Путь к статусу задания', 'status_path', config.status_path ?? 'status', { placeholder: 'status' });
      html += fieldHTML('Путь к имени задания', 'job_name_path', config.job_name_path ?? 'name', { placeholder: 'name' });
      html += fieldHTML('Путь к последнему запуску', 'last_run_path', config.last_run_path ?? 'last_run', { placeholder: 'last_run' });
      html += fieldHTML('Статусы успешного завершения', 'success_values', (config.success_values || ['success', 'succeeded', 'completed', 'ok', 'finished']).join(', '), { span: true, placeholder: 'success, succeeded, completed' });
      html += fieldHTML('Статусы ошибки', 'failure_values', (config.failure_values || ['failed', 'error', 'aborted', 'cancelled', 'timeout']).join(', '), { span: true, placeholder: 'failed, error, aborted' });
      html += fieldHTML('Ожидаемый интервал бэкапа, мин. (0 — не проверять)', 'expected_interval_minutes', config.expected_interval_minutes ?? 0, { type: 'number', min: 0, max: 525600, span: true });
      html += fieldHTML('Проверять TLS-сертификат', 'verify_tls', config.verify_tls !== false, { checkbox: true, span: true });
      html += '<div class="field-span"><small class="field-help">Укажите фактический JSON endpoint и структуру ответа вашей версии Vinchin. Публичный контракт API может отличаться между версиями. Скриптовый вариант — через удалённый агент и файл backup_status_file.</small></div>';
    }
    container.innerHTML = html;
    if (type === 'backup_json') {
      const authSelect = $('[data-config-key="auth_type"]', container);
      authSelect?.addEventListener('change', () => updateAuthFields(authSelect.value));
      updateAuthFields(authSelect?.value || 'basic');
    }
  }

  function updateAuthFields(authType) {
    const username = $('[data-config-key="username"]', $('#sourceDynamicFields'));
    const password = $('[data-config-key="password"]', $('#sourceDynamicFields'));
    const bearer = $('[data-config-key="bearer_token"]', $('#sourceDynamicFields'));
    const showBasic = authType === 'basic';
    const showBearer = authType === 'bearer';
    if (username) username.closest('label').classList.toggle('hidden', !showBasic);
    if (password) password.closest('label').classList.toggle('hidden', !showBasic);
    if (bearer) bearer.closest('label').classList.toggle('hidden', !showBearer);
  }

  function openSourceModal(source = null, initialType = 'vmware') {
    state.editingSource = source;
    $('#sourceId').value = source?.id || '';
    $('#sourceModalTitle').textContent = source ? 'Изменить источник' : 'Добавить источник';
    $('#sourceName').value = source?.name || '';
    $('#sourceType').value = source?.type || initialType;
    $('#sourceType').disabled = !!source;
    $('#sourceSaveButton').innerHTML = source ? 'Сохранить изменения <span>→</span>' : 'Сохранить источник <span>→</span>';
    $('#sourceFormError').classList.add('hidden');
    renderSourceFields($('#sourceType').value, source?.config || {}, source);
    openModal('#sourceModal');
  }

  function collectSourceConfig() {
    const config = {};
    $$('[data-config-key]', $('#sourceDynamicFields')).forEach((field) => {
      const key = field.dataset.configKey;
      if (field.type === 'checkbox') config[key] = field.checked;
      else if (field.type === 'number') config[key] = field.value === '' ? '' : Number(field.value);
      else if (field.tagName === 'SELECT') config[key] = field.value;
      else config[key] = field.value.trim();
    });
    return config;
  }

  async function saveSource(event) {
    event.preventDefault();
    const sourceId = $('#sourceId').value;
    const body = {
      name: $('#sourceName').value.trim(),
      type: $('#sourceType').value,
      enabled: true,
      config: collectSourceConfig(),
    };
    const button = $('#sourceSaveButton');
    button.disabled = true;
    button.textContent = 'Сохранение…';
    $('#sourceFormError').classList.add('hidden');
    try {
      await api(sourceId ? `/api/sources/${encodeURIComponent(sourceId)}` : '/api/sources', {
        method: sourceId ? 'PUT' : 'POST', body: JSON.stringify(body),
      });
      closeModal($('#sourceModal'));
      showToast(sourceId ? 'Источник обновлён' : 'Источник добавлен. Первый опрос начнётся автоматически.', 'success');
      await loadData();
      showView('settings');
    } catch (error) {
      $('#sourceFormError').textContent = error.message;
      $('#sourceFormError').classList.remove('hidden');
    } finally {
      button.disabled = false;
      button.innerHTML = sourceId ? 'Сохранить изменения <span>→</span>' : 'Сохранить источник <span>→</span>';
    }
  }

  function openAgentModal() {
    $('#agentCreatePane').classList.remove('hidden');
    $('#agentTokenPane').classList.add('hidden');
    $('#agentForm').reset();
    $('#agentModalTitle').textContent = 'Подключить агент';
    openModal('#agentModal');
  }

  async function createAgent(event) {
    event.preventDefault();
    const name = $('#agentName').value.trim();
    try {
      const agent = await api('/api/agents', { method: 'POST', body: JSON.stringify({ name }) });
      const config = {
        server_url: agent.server_url,
        agent_id: agent.id,
        token: agent.token,
        interval_seconds: 60,
        verify_tls: true,
        timeout_seconds: 15,
        windows_event_minutes: 15,
        disk_warning_free_percent: 15,
        disk_critical_free_percent: 8,
        monitored_services: [],
        backup_status_file: '',
        external_checks: [],
      };
      $('#agentTokenValue').textContent = agent.token;
      $('#agentConfigValue').textContent = JSON.stringify(config, null, 2);
      $('#agentCreatePane').classList.add('hidden');
      $('#agentTokenPane').classList.remove('hidden');
      $('#agentModalTitle').textContent = 'Токен агента создан';
      await loadData();
    } catch (error) {
      showToast(error.message, 'error');
    }
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
      showToast('Скопировано в буфер обмена', 'success');
    } catch (_) {
      const area = document.createElement('textarea');
      area.value = value;
      document.body.append(area);
      area.select();
      document.execCommand('copy');
      area.remove();
      showToast('Скопировано в буфер обмена', 'success');
    }
  }

  async function pollSource(sourceId, button = null) {
    const previous = button?.textContent;
    if (button) { button.disabled = true; button.textContent = 'Опрос…'; }
    try {
      const response = await api(`/api/sources/${encodeURIComponent(sourceId)}/poll`, { method: 'POST' });
      const status = response.result?.status || 'unknown';
      showToast(`Опрос завершён: ${STATUS_LABELS[status] || status}`, status === 'critical' ? 'error' : status === 'warning' ? 'info' : 'success');
      await loadData();
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      if (button) { button.disabled = false; button.textContent = previous || 'Опросить'; }
    }
  }

  async function removeSource(sourceId) {
    const source = state.overview?.sources.find((item) => item.id === sourceId);
    if (!window.confirm(`Удалить источник «${source?.name || ''}» и его историю?`)) return;
    try {
      await api(`/api/sources/${encodeURIComponent(sourceId)}`, { method: 'DELETE' });
      showToast('Источник и связанные данные удалены', 'success');
      await loadData();
    } catch (error) { showToast(error.message, 'error'); }
  }

  async function revokeAgent(agentId) {
    const agent = state.overview?.agents.find((item) => item.id === agentId);
    if (!window.confirm(`Отключить агента «${agent?.name || ''}»? Его token больше не будет действовать.`)) return;
    try {
      await api(`/api/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' });
      showToast('Агент отключён', 'success');
      await loadData();
    } catch (error) { showToast(error.message, 'error'); }
  }

  async function acknowledgeAlert(alertId, button) {
    const wasAcknowledged = button.dataset.acknowledged === '1';
    try {
      await api(`/api/alerts/${encodeURIComponent(alertId)}`, { method: 'PATCH', body: JSON.stringify({ acknowledged: !wasAcknowledged }) });
      await loadData();
      showToast(wasAcknowledged ? 'Отметка снята' : 'Событие принято в работу', 'success');
    } catch (error) { showToast(error.message, 'error'); }
  }

  function openDeviceDetail(reference) {
    const [type, id] = reference.split(':', 2);
    const device = state.overview?.devices.find((item) => item.entity_type === type && item.id === id);
    if (!device) return;
    $('#detailTitle').textContent = device.name;
    const components = Array.isArray(device.components) ? device.components : [];
    const checks = Array.isArray(device.checks) ? device.checks : [];
    const values = [
      ['Тип', TYPE_LABELS[device.type] || TYPE_LABELS.agent],
      ['Последняя связь', relativeTime(device.last_seen)],
      ['Состояние', STATUS_LABELS[device.status] || 'Нет данных'],
    ];
    const metrics = Object.entries(device.metrics || {}).filter(([key, value]) => value !== null && value !== undefined && typeof value !== 'object' && !key.startsWith('backup_last'));
    let html = `<div class="detail-status">${statusPill(device.status)}<span class="detail-summary">${escapeHTML(device.summary || '')}</span></div><div class="detail-meta">${values.map(([label, value]) => `<span>${escapeHTML(label)}: <b>${escapeHTML(value)}</b></span>`).join('')}${metrics.slice(0, 8).map(([key, value]) => `<span>${escapeHTML(metricLabel(key))}: <b>${escapeHTML(typeof value === 'number' ? shortNumber(value, metricSuffix(key)) : value)}</b></span>`).join('')}</div>`;
    if (device.error) html += `<div class="detail-check"><span class="event-dot critical"></span><div><b>Ошибка подключения</b><small>${escapeHTML(device.error)}</small></div></div>`;
    if (checks.length) {
      html += '<div class="detail-section-title">ПРОВЕРКИ СОСТОЯНИЯ</div>';
      html += checks.slice(0, 70).map((check) => `<div class="detail-check"><span class="event-dot ${escapeHTML(check.status === 'ok' ? 'resolved' : check.status)}"></span><div><b>${escapeHTML(check.name)}</b><small>${escapeHTML(check.message || '')}</small></div></div>`).join('');
    }
    if (components.length) {
      html += `<div class="detail-section-title">КОМПОНЕНТЫ / НОСИТЕЛИ · ${components.length}</div>`;
      html += components.slice(0, 50).map((component) => {
        const name = component.name || component.drive || component.model || 'Компонент';
        const status = component.status || component.health || 'unknown';
        const detail = [component.model, component.vendor, component.free_percent !== undefined ? `Свободно ${component.free_percent}%` : '', component.operational_state?.join(', ')].filter(Boolean).join(' · ');
        return `<div class="detail-check"><span class="event-dot ${escapeHTML(status === 'ok' ? 'resolved' : status)}"></span><div><b>${escapeHTML(name)}</b><small>${escapeHTML(detail || `Состояние: ${status}`)}</small></div></div>`;
      }).join('');
    }
    if (!checks.length && !components.length) html += '<div class="empty-state compact"><small>Подробные измерения появятся после первого отчёта.</small></div>';
    $('#detailContent').innerHTML = html;
    openModal('#detailModal');
  }

  function metricLabel(key) {
    const labels = {
      cpu_percent: 'CPU', memory_percent: 'RAM', disk_min_free_percent: 'Минимум свободно на дисках',
      datastore_min_free_percent: 'Свободно в datastore', temperature_max_c: 'Макс. температура',
      esxi_host_count: 'ESXi-хостов', datastore_count: 'Datastore', hardware_critical_count: 'Проблем компонентов',
      backup_job_count: 'Заданий бэкапа', backup_failed_count: 'Ошибок бэкапа', backup_succeeded_count: 'Успешных бэкапов',
      ipmi_sensor_count: 'IPMI-датчиков', physical_disk_critical_count: 'Проблем дисков',
    };
    return labels[key] || key.replaceAll('_', ' ');
  }
  function metricSuffix(key) { return key.endsWith('_percent') || key.includes('temperature') ? (key.includes('temperature') ? '°C' : '%') : ''; }

  async function savePollSettings(event) {
    event.preventDefault();
    const value = Number($('#pollInterval').value);
    try {
      const saved = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ poll_interval_seconds: value }) });
      state.settings.poll_interval_seconds = saved.poll_interval_seconds;
      $('#settingsSaved').classList.remove('hidden');
      window.setTimeout(() => $('#settingsSaved').classList.add('hidden'), 2500);
      showToast('Интервал опроса обновлён', 'success');
      await loadData();
    } catch (error) { showToast(error.message, 'error'); }
  }

  function applyDashboardWidgets() {
    let preferences = {};
    try { preferences = JSON.parse(localStorage.getItem('srvmon-dashboard-widgets') || '{}'); } catch (_) { preferences = {}; }
    $$('[data-widget-toggle]').forEach((checkbox) => {
      const key = checkbox.dataset.widgetToggle;
      if (Object.hasOwn(preferences, key)) checkbox.checked = preferences[key];
    });
    $$('[data-widget]').forEach((element) => {
      const key = element.dataset.widget;
      const visible = Object.hasOwn(preferences, key) ? preferences[key] : true;
      element.classList.toggle('widget-hidden', !visible);
    });
  }

  async function boot() {
    try {
      const auth = await api('/api/auth/me');
      if (!auth.authenticated) { showLogin(); return; }
      showApp(auth.username || 'admin');
      applyDashboardWidgets();
      await loadData();
      window.setInterval(loadData, 15000);
    } catch (_) {
      showLogin();
    }
  }

  $('#loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const submit = $('button[type="submit"]', event.currentTarget);
    submit.disabled = true;
    try {
      const response = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: $('#loginUsername').value.trim(), password: $('#loginPassword').value }) });
      showApp(response.username || 'admin');
      applyDashboardWidgets();
      await loadData();
    } catch (error) {
      showLogin(error.message);
    } finally { submit.disabled = false; }
  });

  $('#logoutButton').addEventListener('click', async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* session may already be gone */ }
    $('#profileMenu').classList.add('hidden');
    showLogin();
  });
  $('#profileButton').addEventListener('click', () => $('#profileMenu').classList.toggle('hidden'));
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.profile-wrap')) $('#profileMenu').classList.add('hidden');
  });
  $('#refreshButton').addEventListener('click', loadData);
  $('#trendRange').addEventListener('change', async () => {
    try { state.history = await api(`/api/history?hours=${$('#trendRange').value}&limit=1000`); renderChart(); }
    catch (error) { showToast(error.message, 'error'); }
  });
  $('#deviceSearch').addEventListener('input', () => renderDeviceTable(state.overview?.devices || [], $('#deviceSearch').value));
  $('#assetSearch').addEventListener('input', renderAssets);
  $('#alertFilter').addEventListener('change', renderAlertPage);
  $('#pollSettingsForm').addEventListener('submit', savePollSettings);
  $('#sourceForm').addEventListener('submit', saveSource);
  $('#agentForm').addEventListener('submit', createAgent);
  $('#sourceType').addEventListener('change', (event) => renderSourceFields(event.target.value));
  $('#addSourceButton').addEventListener('click', () => openSourceModal());
  $('#assetsAddSource').addEventListener('click', () => openSourceModal());
  $('#settingsAddSource').addEventListener('click', () => showView('settings'));
  $('#settingsAddSourceTop').addEventListener('click', () => openSourceModal());
  $('#settingsAddBackup').addEventListener('click', () => openSourceModal(null, 'backup_json'));
  $('#addAgentButton').addEventListener('click', openAgentModal);
  $('#assetsAddAgent').addEventListener('click', openAgentModal);
  $('#settingsAddAgent').addEventListener('click', openAgentModal);
  $('#customizeButton').addEventListener('click', () => $('#customizePanel').classList.toggle('hidden'));
  $$('[data-widget-toggle]').forEach((checkbox) => checkbox.addEventListener('change', () => {
    const preferences = {};
    $$('[data-widget-toggle]').forEach((item) => { preferences[item.dataset.widgetToggle] = item.checked; });
    localStorage.setItem('srvmon-dashboard-widgets', JSON.stringify(preferences));
    applyDashboardWidgets();
  }));
  $$('[data-view]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
  $$('[data-view-link]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.viewLink)));
  $$('[data-asset-filter]').forEach((button) => button.addEventListener('click', () => {
    state.assetFilter = button.dataset.assetFilter;
    $$('[data-asset-filter]').forEach((tab) => tab.classList.toggle('active', tab === button));
    renderAssets();
  }));
  $$('[data-close-modal]').forEach((button) => button.addEventListener('click', () => closeModal(button.closest('.modal-backdrop'))));
  $$('.modal-backdrop').forEach((backdrop) => backdrop.addEventListener('click', (event) => { if (event.target === backdrop) closeModal(backdrop); }));
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') $$('.modal-backdrop:not(.hidden)').forEach(closeModal); });
  $('#copyTokenButton').addEventListener('click', () => copyText($('#agentTokenValue').textContent));
  $('#copyConfigButton').addEventListener('click', () => copyText($('#agentConfigValue').textContent));
  $('#alertFilter').addEventListener('change', renderAlertPage);

  document.addEventListener('click', (event) => {
    const detail = event.target.closest('[data-device-detail]');
    if (detail) openDeviceDetail(detail.dataset.deviceDetail);
    const edit = event.target.closest('[data-source-edit]');
    if (edit) {
      const source = state.overview?.sources.find((item) => item.id === edit.dataset.sourceEdit);
      if (source) openSourceModal(source);
    }
    const poll = event.target.closest('[data-source-poll]');
    if (poll) pollSource(poll.dataset.sourcePoll, poll);
    const remove = event.target.closest('[data-source-delete]');
    if (remove) removeSource(remove.dataset.sourceDelete);
    const revoke = event.target.closest('[data-agent-revoke]');
    if (revoke) revokeAgent(revoke.dataset.agentRevoke);
    const acknowledge = event.target.closest('[data-alert-ack]');
    if (acknowledge) acknowledgeAlert(acknowledge.dataset.alertAck, acknowledge);
  });

  boot();
})();
