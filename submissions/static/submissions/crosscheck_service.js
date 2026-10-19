(() => {
    const connection = document.querySelector('[data-crosscheck-connection]');
    if (connection) {
        const button = connection.querySelector('button');
        const status = connection.querySelector('[data-connection-status]');
        const url = document.getElementById('id_crosscheck_service_url');
        const folder = document.getElementById('id_crosscheck_service_folder');
        const folderPicker = window.TomSelect ? new window.TomSelect(folder, {
            create: false,
            maxItems: 1,
            maxOptions: null,
            closeAfterSelect: true,
            render: {
                option: (data, escape) => `<div>${escape(data.text)}</div>`,
                item: (data, escape) => `<div>${escape(data.text)}</div>`,
            },
        }) : null;
        if (folderPicker) folderPicker.wrapper.classList.add('cfm-paper-picker', 'cfm-folder-picker');
        url.addEventListener('input', () => { status.textContent = 'Not checked'; status.className = 'text-muted'; });
        button.addEventListener('click', async () => {
            button.disabled = true;
            status.textContent = 'Checking service and IEEE folders…';
            const body = new FormData();
            body.set('url', url.value);
            body.set('csrfmiddlewaretoken', document.querySelector('[name=csrfmiddlewaretoken]').value);
            try {
                const response = await fetch(connection.dataset.crosscheckConnection, {method: 'POST', body});
                const data = await response.json();
                if (!response.ok || !data.available) throw new Error(data.message || 'Connection failed.');
                const selectedFolder = folder.value;
                if (folderPicker) {
                    folderPicker.clearOptions();
                    folderPicker.addOptions(data.folders.map(name => ({value: name, text: name})));
                    folderPicker.refreshOptions(false);
                } else {
                    const names = [...new Set([selectedFolder, ...data.folders].filter(Boolean))];
                    folder.replaceChildren(new Option('Select a conference folder', ''),
                        ...names.map(name => new Option(name, name)));
                    folder.value = selectedFolder;
                }
                status.textContent = `Ready · IEEE account: ${data.username} · ${data.folders.length} folders`;
                status.className = 'text-success';
                if (folder.value && !data.folders.includes(folder.value)) {
                    status.textContent += ' · Selected folder is unavailable'; status.className = 'text-warning';
                }
            } catch (error) { status.textContent = error.message; status.className = 'text-danger'; }
            finally { button.disabled = false; }
        });
    }
    const job = document.querySelector('[data-crosscheck-job]');
    if (!job || job.dataset.status !== 'running') return;
    const form = job.querySelector('[data-job-refresh]');
    const countdown = job.querySelector('[data-job-countdown]');
    const interval = 15000;
    let timer;
    let nextUpdate;
    let updating = false;
    let stopped = false;
    countdown.hidden = false;

    function tick() {
        clearTimeout(timer);
        if (stopped || updating) return;
        if (document.hidden) {
            countdown.textContent = 'Auto-update paused while this tab is hidden.';
            return;
        }
        const seconds = Math.max(0, Math.ceil((nextUpdate - Date.now()) / 1000));
        if (seconds === 0) { refresh(); return; }
        countdown.textContent = `Next update in ${seconds}s`;
        timer = setTimeout(tick, 1000);
    }

    function schedule() {
        nextUpdate = Date.now() + interval;
        tick();
    }

    async function refresh() {
        updating = true;
        countdown.textContent = 'Checking progress…';
        const body = new FormData(form); body.set('action', 'refresh');
        try {
            const response = await fetch(location.href, {method: 'POST', body, headers: {Accept: 'application/json'}});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Progress check failed.');
            if (data.status !== 'running') {
                stopped = true;
                countdown.textContent = 'Updating batch page…';
                location.reload();
                return;
            }
            const p = data.progress;
            job.querySelector('[data-job-message]').textContent = `Uploads confirmed: ${p.succeeded || 0} · Reports ready: ${p.ready || 0} · Downloaded: ${p.downloaded || 0}`;
        } catch (error) {
            stopped = true;
            countdown.textContent = 'Auto-update paused. Use Check progress to resume.';
            job.querySelector('[data-job-message]').textContent = `${error.message} Use Check progress to resume.`;
        } finally {
            updating = false;
            if (!stopped) schedule();
        }
    }
    document.addEventListener('visibilitychange', () => {
        if (!stopped && !updating) schedule();
    });
    form.addEventListener('submit', () => {
        stopped = true;
        clearTimeout(timer);
        countdown.textContent = 'Checking progress…';
    });
    schedule();
})();
