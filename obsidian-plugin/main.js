const { Plugin, Notice, MarkdownView, PluginSettingTab, Setting, ItemView, Modal } = require("obsidian");
const fs = require("fs");
const path = require("path");
const { exec, execSync } = require("child_process");

function fuzzyMatch(query, target) {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0, score = 0, lastIdx = -1;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      score += ti === lastIdx + 1 ? 0 : ti - (lastIdx + 1);
      lastIdx = ti;
      qi++;
    }
  }
  return qi === q.length ? score : -1;
}

const TYPE_OPTIONS = ["note", "task", "job"];
const HEARTBEAT_MAX_AGE_S = 30;
const HISTORY_VIEW_TYPE = "davyjones-history";
const CONTROL_VIEW_TYPE = "davyjones-control";
const REPORTS_VIEW_TYPE = "davyjones-reports";
const CALENDAR_VIEW_TYPE = "davyjones-calendar";
const HISTORY_PAGE_SIZE = 20;
const REPORTS_PAGE_SIZE = 20;
const CAL_COLORS = ["#7c3aed", "#2563eb", "#059669", "#d97706", "#dc2626", "#0891b2"];

class DavyJonesPlugin extends Plugin {
  async onload() {
    this._vaultPath = this.app.vault.adapter.basePath;
    this._projectRoot = this._loadProjectRoot();
    this._gitDirty = false;
    this._gitChangeCount = 0;
    this._committing = false;
    this._commitDone = false;
    this._commitDoneTimer = null;

    // Commit
    this.addRibbonIcon("git-commit-horizontal", "DavyJones: Commit", () => this.commitChanges());
    this.addCommand({ id: "commit-changes", name: "Commit vault changes", callback: () => this.commitChanges() });

    // Switch vault command
    this.addCommand({ id: "switch-vault", name: "Switch active vault to this one", callback: () => this.switchToThisVault() });

    // History panel
    this.registerView(HISTORY_VIEW_TYPE, (leaf) => new DavyJonesHistoryView(leaf, this));
    this.addRibbonIcon("git-branch", "DavyJones: Vault History", () => this._activateHistoryView());
    this.addCommand({ id: "toggle-history", name: "Toggle vault history panel", callback: () => this._activateHistoryView() });

    // Control panel
    this.registerView(CONTROL_VIEW_TYPE, (leaf) => new DavyJonesControlPanel(leaf, this));
    this.addRibbonIcon("sliders-horizontal", "DavyJones: Control Panel", () => this._activateControlPanel());
    this.addCommand({ id: "open-control-panel", name: "Open control panel", callback: () => this._activateControlPanel() });

    // Task modal
    this.addRibbonIcon("send", "DavyJones: New Task", () => new DavyJonesTaskModal(this.app, this).open());
    this.addCommand({ id: "new-task", name: "Send a new task to agents", callback: () => new DavyJonesTaskModal(this.app, this).open() });

    // Reports panel
    this.registerView(REPORTS_VIEW_TYPE, (leaf) => new DavyJonesReportsView(leaf, this));
    this.addRibbonIcon("file-text", "DavyJones: Agent Reports", () => this._activateReportsView());
    this.addCommand({ id: "open-reports", name: "View agent execution reports", callback: () => this._activateReportsView() });

    // Calendar
    this.registerView(CALENDAR_VIEW_TYPE, (leaf) => new DavyJonesCalendarView(leaf, this));
    this.addRibbonIcon("calendar", "DavyJones: Calendar", () => this._activateCalendarView());
    this.addCommand({ id: "open-calendar", name: "Open calendar", callback: () => this._activateCalendarView() });
    this.addCommand({ id: "import-ics", name: "Import .ics calendar file", callback: () => new DavyJonesICSImportModal(this.app, this).open() });

    // Status bar: connection indicator
    this._statusBarEl = this.addStatusBarItem();
    this._statusBarEl.addClass("davyjones-statusbar");

    // Status bar: git commit button
    this._gitBarEl = this.addStatusBarItem();
    this._gitBarEl.addClass("davyjones-gitbar");
    this._gitBarEl.addEventListener("click", () => this.commitChanges());

    this._updateStatusBar();
    this._updateGitStatus();

    // Auto-add frontmatter to new notes
    this.registerEvent(this.app.vault.on("create", (file) => this.onFileCreate(file)));

    // Refresh git status on file changes
    this.registerEvent(this.app.vault.on("modify", () => this._debouncedGitCheck()));
    this.registerEvent(this.app.vault.on("delete", () => this._debouncedGitCheck()));
    this.registerEvent(this.app.vault.on("rename", () => this._debouncedGitCheck()));

    // Render UI on note change
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.renderUI()));
    this.registerEvent(this.app.metadataCache.on("changed", () => {
      clearTimeout(this._uiTimer);
      this._uiTimer = setTimeout(() => this.renderUI(), 100);
    }));
    this.app.workspace.onLayoutReady(() => this.renderUI());

    // Settings tab
    this.addSettingTab(new DavyJonesSettingTab(this.app, this));

    // Periodic refresh
    this.registerInterval(window.setInterval(() => {
      this._updateStatusBar();
      this._updateGitStatus();
      // Re-render nav bar if Obsidian silently removed it (e.g. mode switch)
      const activeView = this.app.workspace.getActiveViewOfType(MarkdownView);
      if (activeView && activeView.file && !activeView.contentEl.querySelector(".davyjones-nav")) {
        this.renderUI();
      }
      // Refresh history panel if open
      for (const leaf of this.app.workspace.getLeavesOfType(HISTORY_VIEW_TYPE)) {
        if (leaf.view && leaf.view.refresh) leaf.view.refresh();
      }
      // Refresh reports panel if open
      for (const leaf of this.app.workspace.getLeavesOfType(REPORTS_VIEW_TYPE)) {
        if (leaf.view && leaf.view.refresh) leaf.view.refresh();
      }
      // Refresh calendar if open
      for (const leaf of this.app.workspace.getLeavesOfType(CALENDAR_VIEW_TYPE)) {
        if (leaf.view && leaf.view.refresh) leaf.view.refresh();
      }
    }, 15000));
  }

  onunload() {
    document.querySelectorAll(".davyjones-nav").forEach((el) => el.remove());
    this.app.workspace.detachLeavesOfType(HISTORY_VIEW_TYPE);
    this.app.workspace.detachLeavesOfType(CONTROL_VIEW_TYPE);
    this.app.workspace.detachLeavesOfType(REPORTS_VIEW_TYPE);
    this.app.workspace.detachLeavesOfType(CALENDAR_VIEW_TYPE);
  }

  async _activateHistoryView() {
    const existing = this.app.workspace.getLeavesOfType(HISTORY_VIEW_TYPE);
    if (existing.length) {
      this.app.workspace.revealLeaf(existing[0]);
      return;
    }
    const leaf = this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({ type: HISTORY_VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
  }

  async _activateControlPanel() {
    const existing = this.app.workspace.getLeavesOfType(CONTROL_VIEW_TYPE);
    if (existing.length) {
      this.app.workspace.revealLeaf(existing[0]);
      return;
    }
    const leaf = this.app.workspace.getLeaf("tab");
    await leaf.setViewState({ type: CONTROL_VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
  }

  async _activateReportsView() {
    const existing = this.app.workspace.getLeavesOfType(REPORTS_VIEW_TYPE);
    if (existing.length) {
      this.app.workspace.revealLeaf(existing[0]);
      return;
    }
    const leaf = this.app.workspace.getRightLeaf(false);
    await leaf.setViewState({ type: REPORTS_VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
  }

  async _activateCalendarView() {
    const existing = this.app.workspace.getLeavesOfType(CALENDAR_VIEW_TYPE);
    if (existing.length) {
      this.app.workspace.revealLeaf(existing[0]);
      return;
    }
    const leaf = this.app.workspace.getLeaf("tab");
    await leaf.setViewState({ type: CALENDAR_VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
  }

  // ─── Config ──────────────────────────────────────────────────

  _loadProjectRoot() {
    try {
      const configPath = path.join(this._vaultPath, ".obsidian", "davyjones-config.json");
      const data = JSON.parse(fs.readFileSync(configPath, "utf8"));
      return data.projectRoot || null;
    } catch {
      return null;
    }
  }

  // ─── .davyjones.env read/write ──────────────────────────────

  _readDavyJonesEnv() {
    const envPath = path.join(this._vaultPath, ".davyjones.env");
    const config = {};
    try {
      const content = fs.readFileSync(envPath, "utf8");
      for (const line of content.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#")) continue;
        const eqIdx = trimmed.indexOf("=");
        if (eqIdx < 0) continue;
        const key = trimmed.slice(0, eqIdx).trim();
        const value = trimmed.slice(eqIdx + 1).trim();
        if (key) config[key] = value;
      }
    } catch {
      // File doesn't exist yet
    }
    return config;
  }

  _writeDavyJonesEnv(config) {
    const envPath = path.join(this._vaultPath, ".davyjones.env");
    const lines = [
      "# DavyJones vault configuration",
      "# Managed by the DavyJones Obsidian plugin — edit via Settings > DavyJones",
      "",
    ];

    const sections = [
      { header: "# Claude Auth", keys: ["CLAUDE_CODE_OAUTH_TOKEN"] },
      { header: "# GitHub", keys: ["GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_MCP_ENABLED"] },
      { header: "# GitLab", keys: ["GITLAB_TOKEN", "GITLAB_MCP_URL", "GITLAB_MCP_ENABLED"] },
      { header: "# Slack", keys: ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_MCP_ENABLED"] },
    ];

    for (const section of sections) {
      const hasValues = section.keys.some((k) => config[k]);
      if (hasValues) {
        lines.push(section.header);
        for (const key of section.keys) {
          if (config[key] !== undefined && config[key] !== "") {
            lines.push(`${key}=${config[key]}`);
          }
        }
        lines.push("");
      }
    }

    fs.writeFileSync(envPath, lines.join("\n"), "utf8");
  }

  _readVaultRules() {
    const rulesPath = path.join(this._vaultPath, ".davyjones-rules.json");
    try {
      return JSON.parse(fs.readFileSync(rulesPath, "utf8"));
    } catch {
      return {
        customInstructions: "",
        verbosity: "normal",
        maxTurns: 20,
        timeout: 300,
        autoCommit: false,
        ignorePatterns: [],
        allowedOperations: { createFiles: true, deleteFiles: true, modifyFiles: true, runGitCommands: true },
      };
    }
  }

  _writeVaultRules(rules) {
    const rulesPath = path.join(this._vaultPath, ".davyjones-rules.json");
    fs.writeFileSync(rulesPath, JSON.stringify(rules, null, 2), "utf8");
  }

  _readCalendar() {
    const calPath = path.join(this._vaultPath, ".davyjones-calendar.json");
    try {
      return JSON.parse(fs.readFileSync(calPath, "utf8"));
    } catch {
      return {
        version: 1,
        calendars: [{ id: "default", name: "Default", color: "#7c3aed", source: "local" }],
        events: [],
      };
    }
  }

  _writeCalendar(data) {
    const calPath = path.join(this._vaultPath, ".davyjones-calendar.json");
    fs.writeFileSync(calPath, JSON.stringify(data, null, 2), "utf8");
  }

  _applyServiceConfig() {
    if (!this._projectRoot) {
      new Notice("DavyJones project path not configured.");
      return;
    }

    new Notice("Applying service configuration...");

    const shell = process.env.SHELL || "/bin/bash";
    const mergeScript = path.join(this._projectRoot, "scripts", "_merge_env.sh");
    const rootEnv = path.join(this._projectRoot, ".env");

    // Build profile flags for services that have tokens AND are enabled
    const config = this._readDavyJonesEnv();
    const rules = this._readVaultRules();
    const profiles = [];
    if (config.SLACK_BOT_TOKEN && config.SLACK_MCP_ENABLED !== "false") profiles.push("--profile slack");
    if (config.GITLAB_TOKEN && config.GITLAB_MCP_ENABLED !== "false") profiles.push("--profile gitlab");
    if (config.GITHUB_TOKEN && config.GITHUB_MCP_ENABLED !== "false") profiles.push("--profile github");
    if (rules.autoCommit) profiles.push("--profile auto-commit");
    const profileFlags = profiles.join(" ");

    const cmd = `${shell} -l -c 'source "${mergeScript}" && merge_vault_env "${this._vaultPath}" "${rootEnv}" && cd "${this._projectRoot}" && docker compose --profile build-only ${profileFlags} build && docker compose ${profileFlags} up -d 2>&1'`;

    exec(cmd, { timeout: 180000, cwd: this._projectRoot }, (err, stdout, stderr) => {
      if (err) {
        new Notice("Apply failed: " + (stderr?.split("\n").pop() || err.message));
        return;
      }
      new Notice("Configuration applied and dispatcher restarted.");
      this._updateStatusBar();
    });
  }

  // ─── Heartbeat check ────────────────────────────────────────

  _readHeartbeat() {
    try {
      const hbPath = path.join(this._vaultPath, ".davyjones");
      const raw = JSON.parse(fs.readFileSync(hbPath, "utf8"));
      const age = (Date.now() / 1000) - raw.ts;
      return { active: age < HEARTBEAT_MAX_AGE_S, creds: raw.creds || null, raw };
    } catch {
      return { active: false, creds: null, raw: null };
    }
  }

  _isVaultActive() {
    return this._readHeartbeat().active;
  }

  // ─── Git status ──────────────────────────────────────────────

  _debouncedGitCheck() {
    clearTimeout(this._gitTimer);
    this._gitTimer = setTimeout(() => this._updateGitStatus(), 1500);
  }

  _updateGitStatus() {
    if (this._committing) return;
    try {
      const out = execSync("git status --porcelain", {
        cwd: this._vaultPath,
        encoding: "utf8",
        timeout: 5000,
      }).trim();
      const lines = out ? out.split("\n").length : 0;
      this._gitDirty = lines > 0;
      this._gitChangeCount = lines;
    } catch {
      this._gitDirty = false;
      this._gitChangeCount = 0;
    }
    this._renderGitBar();
  }

  _renderGitBar() {
    if (!this._gitBarEl) return;
    this._gitBarEl.empty();

    // State: working (blue pulsing)
    if (this._committing) {
      this._gitBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-commit" });
      this._gitBarEl.createEl("span", { text: "committing...", cls: "davyjones-statusbar-text" });
      this._gitBarEl.style.cursor = "default";
      return;
    }

    // State: dirty (yellow)
    if (this._gitDirty) {
      clearTimeout(this._commitDoneTimer);
      this._commitDone = false;
      this._gitBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-dirty" });
      this._gitBarEl.createEl("span", {
        text: `${this._gitChangeCount} change${this._gitChangeCount !== 1 ? "s" : ""}`,
        cls: "davyjones-statusbar-text",
      });
      this._gitBarEl.style.cursor = "pointer";
      return;
    }

    // State: done (green, auto-fades after 3s)
    if (this._commitDone) {
      this._gitBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-clean" });
      this._gitBarEl.createEl("span", { text: "committed", cls: "davyjones-statusbar-text" });
      this._gitBarEl.style.cursor = "default";
      return;
    }

    // No changes, no recent commit — hide
    this._gitBarEl.style.cursor = "default";
  }

  // ─── Commit ──────────────────────────────────────────────────

  async commitChanges() {
    if (this._committing) return;
    if (!this._gitDirty) {
      new Notice("Nothing to commit");
      return;
    }

    this._committing = true;
    this._renderGitBar();

    const cmd = 'git add -A && git -c user.name="Vault Owner" -c user.email="vault-owner@local" commit -m "vault update"';
    exec(cmd, { cwd: this._vaultPath, timeout: 15000 }, (err, stdout, stderr) => {
      this._committing = false;
      if (err) {
        console.error("DavyJones commit error:", stderr || err.message);
        new Notice("Commit failed: " + (stderr?.split("\n")[0] || err.message));
      } else {
        new Notice("Changes committed");
        // Show green "committed" for 3 seconds then fade
        this._commitDone = true;
        clearTimeout(this._commitDoneTimer);
        this._commitDoneTimer = setTimeout(() => {
          this._commitDone = false;
          this._renderGitBar();
        }, 3000);
      }
      this._updateGitStatus();
      this._updateStatusBar();
    });
  }

  // ─── Status bar (connection + credential status) ────────────

  _updateStatusBar() {
    if (!this._statusBarEl) return;
    const hb = this._readHeartbeat();
    const active = hb.active;
    const creds = hb.creds;
    this._statusBarEl.empty();

    // State 1: Dispatcher not running
    if (!active) {
      this._statusBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-off" });
      this._statusBarEl.createEl("span", {
        text: "DavyJones (offline)",
        cls: "davyjones-statusbar-text",
      });
      this._statusBarEl.style.cursor = "pointer";
      this._statusBarEl.onclick = () => this.switchToThisVault();
      return;
    }

    // State 2: Auth expired — refresh token dead
    if (creds && creds.status === "auth_expired") {
      this._statusBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-auth-error" });
      this._statusBarEl.createEl("span", {
        text: "DavyJones (auth expired)",
        cls: "davyjones-statusbar-text",
      });
      this._statusBarEl.style.cursor = "pointer";
      this._statusBarEl.onclick = () => this._fixCredentials();
      return;
    }

    // State 2b: No credentials found
    if (creds && creds.status === "no_credentials") {
      this._statusBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-auth-error" });
      this._statusBarEl.createEl("span", {
        text: "DavyJones (no creds)",
        cls: "davyjones-statusbar-text",
      });
      this._statusBarEl.style.cursor = "pointer";
      this._statusBarEl.onclick = () => this._fixCredentials();
      return;
    }

    // State 3: Transient refresh failure
    if (creds && creds.status === "refresh_failed") {
      this._statusBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-warn" });
      this._statusBarEl.createEl("span", {
        text: "DavyJones (cred warning)",
        cls: "davyjones-statusbar-text",
      });
      this._statusBarEl.style.cursor = "pointer";
      this._statusBarEl.onclick = () => this._fixCredentials();
      return;
    }

    // State 4: Token approaching expiry (informational)
    if (creds && creds.status === "expiring") {
      this._statusBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-warn" });
      this._statusBarEl.createEl("span", {
        text: "DavyJones",
        cls: "davyjones-statusbar-text",
      });
      this._statusBarEl.style.cursor = "default";
      this._statusBarEl.onclick = null;
      return;
    }

    // State 5: Healthy
    this._statusBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-on" });
    this._statusBarEl.createEl("span", {
      text: "DavyJones",
      cls: "davyjones-statusbar-text",
    });
    this._statusBarEl.style.cursor = "default";
    this._statusBarEl.onclick = null;
  }

  // ─── Switch vault ────────────────────────────────────────────

  async switchToThisVault() {
    if (!this._projectRoot) {
      new Notice("DavyJones project path not configured. Run scripts/setup.sh first.");
      return;
    }
    if (this._isVaultActive()) {
      new Notice("This vault is already active.");
      return;
    }

    new Notice("Switching vault...");
    const switchScript = path.join(this._projectRoot, "scripts", "switch.sh");
    const shell = process.env.SHELL || "/bin/bash";
    const cmd = `${shell} -l -c 'bash "${switchScript}" "${this._vaultPath}"'`;

    exec(cmd, { timeout: 60000, cwd: this._projectRoot }, (err, stdout, stderr) => {
      if (err || !stdout.includes("Done.")) {
        const errMsg = stderr?.trim() || stdout?.trim() || err?.message || "Unknown error";
        new Notice("Switch failed: " + errMsg.split("\n").pop());
        return;
      }
      new Notice("Vault switched!");
      this._updateStatusBar();
      let polls = 0;
      const fastPoll = setInterval(() => {
        this._updateStatusBar();
        polls++;
        if (this._isVaultActive() || polls >= 15) clearInterval(fastPoll);
      }, 2000);
    });
  }

  // ─── Fix credentials ───────────────────────────────────────

  async _fixCredentials() {
    if (!this._projectRoot) {
      new Notice("DavyJones project path not configured. Run scripts/setup.sh first.");
      return;
    }

    new Notice("Fixing credentials — a Terminal window will open for login...", 8000);

    const shell = process.env.SHELL || "/bin/bash";
    const fixScript = path.join(this._projectRoot, "scripts", "fix_credentials.sh");
    const cmd = `${shell} -l -c 'bash "${fixScript}" && cd "${this._projectRoot}" && docker compose up -d dispatcher'`;

    // Longer timeout: claude login waits for browser OAuth callback
    exec(cmd, { timeout: 300000, cwd: this._projectRoot }, (err, stdout, stderr) => {
      if (err) {
        const errMsg = stderr?.trim() || err.message;
        const output = stdout?.trim() || "";
        if (output.includes("FIX_NO_CLI")) {
          new Notice("Claude CLI not found. Install it or run 'claude login' manually.");
        } else if (errMsg.includes("Could not find")) {
          new Notice("No Claude credentials in Keychain. Run 'claude login' in terminal first.");
        } else {
          new Notice("Fix failed: " + errMsg.split("\n").pop());
        }
        return;
      }

      const output = stdout?.trim() || "";
      if (output.includes("FIX_EXTRACTED")) {
        new Notice("Credentials restored from Keychain.");
      } else if (output.includes("FIX_REFRESHED")) {
        new Notice("Token refreshed successfully.");
      } else if (output.includes("FIX_RELOGIN")) {
        new Notice("Re-authenticated via Claude login.");
      } else {
        new Notice("Credentials fixed.");
      }

      // Fast-poll until heartbeat reports healthy credentials
      let polls = 0;
      const fastPoll = setInterval(() => {
        this._updateStatusBar();
        polls++;
        const hb = this._readHeartbeat();
        if ((hb.creds && hb.creds.status === "ok") || polls >= 15) {
          clearInterval(fastPoll);
        }
      }, 2000);
    });
  }

  // ─── Auto frontmatter ──────────────────────────────────────

  async onFileCreate(file) {
    if (!file.path.endsWith(".md")) return;
    await new Promise((r) => setTimeout(r, 200));
    const content = await this.app.vault.read(file);
    if (content.trimStart().startsWith("---")) return;
    const today = new Date().toISOString().slice(0, 10);
    const fm = `---\ntype: note\ncreated: ${today}\n---\n\n`;
    await this.app.vault.modify(file, fm + content);
  }

  // ─── Combined UI: property bar + search ────────────────────

  renderUI() {
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view || !view.file) return;

    const file = view.file;
    const contentEl = view.contentEl;
    contentEl.querySelector(".davyjones-nav")?.remove();

    const cache = this.app.metadataCache.getFileCache(file);
    const fm = cache?.frontmatter || {};

    const nav = createDiv({ cls: "davyjones-nav" });
    const props = nav.createDiv({ cls: "davyjones-props" });

    // Type badge (clickable to cycle)
    const typeVal = fm.type || "note";
    const typeBadge = props.createEl("span", {
      cls: `davyjones-prop-badge davyjones-t-${typeVal}`,
      text: typeVal,
    });
    typeBadge.addEventListener("click", async () => {
      const currentIdx = TYPE_OPTIONS.indexOf(typeVal);
      const nextType = TYPE_OPTIONS[(currentIdx + 1) % TYPE_OPTIONS.length];
      await this.app.fileManager.processFrontMatter(file, (f) => { f.type = nextType; });
    });

    // Status badge (if present)
    if (fm.status) {
      props.createEl("span", {
        cls: `davyjones-prop-badge davyjones-s-${fm.status}`,
        text: fm.status.replace("_", " "),
      });
    }

    // Tags
    const tags = fm.tags || [];
    if (tags.length) {
      const tagGroup = props.createEl("span", { cls: "davyjones-prop-tags" });
      tags.forEach((t) => tagGroup.createEl("span", { cls: "davyjones-prop-tag", text: t }));
    }

    // Created date
    if (fm.created) {
      props.createEl("span", { cls: "davyjones-prop-date", text: fm.created });
    }

    // Spacer
    props.createDiv({ cls: "davyjones-spacer" });

    // Inline search
    const searchWrap = props.createDiv({ cls: "davyjones-search-wrapper" });
    const input = searchWrap.createEl("input", {
      type: "text",
      placeholder: "Go to file...",
      cls: "davyjones-search",
    });
    const results = searchWrap.createDiv({ cls: "davyjones-results" });
    this._bindSearch(input, results);

    contentEl.prepend(nav);
  }

  // ─── Search logic ──────────────────────────────────────────

  _bindSearch(input, results) {
    let selectedIdx = -1;

    const renderResults = (query) => {
      results.empty();
      selectedIdx = -1;
      if (!query) { results.classList.remove("is-visible"); return; }

      const scored = [];
      for (const f of this.app.vault.getMarkdownFiles()) {
        const s1 = fuzzyMatch(query, f.basename);
        const s2 = fuzzyMatch(query, f.path);
        const best = s1 >= 0 && s2 >= 0 ? Math.min(s1, s2) : s1 >= 0 ? s1 : s2;
        if (best >= 0) scored.push({ file: f, score: best });
      }
      scored.sort((a, b) => a.score - b.score);
      const matches = scored.slice(0, 15);

      if (!matches.length) { results.classList.remove("is-visible"); return; }
      results.classList.add("is-visible");

      for (const { file: f } of matches) {
        const item = results.createDiv({ cls: "davyjones-item" });
        const c = this.app.metadataCache.getFileCache(f);
        const meta = c?.frontmatter || {};
        const row = item.createDiv({ cls: "davyjones-item-row" });
        row.createEl("span", { cls: "davyjones-item-name", text: f.basename });
        if (meta.status) {
          row.createEl("span", { cls: `davyjones-item-status davyjones-s-${meta.status}`, text: meta.status });
        }
        if (f.parent?.path) {
          row.createEl("span", { cls: "davyjones-item-path", text: f.parent.path });
        }
        item.addEventListener("click", () => {
          this.app.workspace.openLinkText(f.path, "", false);
          input.value = "";
          results.empty();
          results.classList.remove("is-visible");
        });
      }
    };

    input.addEventListener("input", (e) => renderResults(e.target.value));
    input.addEventListener("keydown", (e) => {
      const items = results.querySelectorAll(".davyjones-item");
      if (!items.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        selectedIdx = Math.min(selectedIdx + 1, items.length - 1);
        items.forEach((el, i) => el.classList.toggle("is-selected", i === selectedIdx));
        items[selectedIdx]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        selectedIdx = Math.max(selectedIdx - 1, 0);
        items.forEach((el, i) => el.classList.toggle("is-selected", i === selectedIdx));
        items[selectedIdx]?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (selectedIdx >= 0) items[selectedIdx]?.click();
        else items[0]?.click();
      } else if (e.key === "Escape") {
        input.value = "";
        results.empty();
        results.classList.remove("is-visible");
        input.blur();
      }
    });
    input.addEventListener("blur", () => {
      setTimeout(() => { results.empty(); results.classList.remove("is-visible"); }, 150);
    });
  }
}

// ─── Task Modal ───────────────────────────────────────────────

class DavyJonesTaskModal extends Modal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this._scope = "vault";
    this._maxTurns = 20;
    this._verbosity = "normal";
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass("davyjones-task-modal");

    contentEl.createEl("h2", { text: "Send Task to DavyJones" });

    // Task description
    contentEl.createEl("label", { text: "What should the agents do?", cls: "davyjones-tm-label" });
    this._textarea = contentEl.createEl("textarea", {
      cls: "davyjones-tm-textarea",
      attr: { rows: "6", placeholder: "Describe the task..." },
    });

    // Scope selector
    const scopeSection = contentEl.createDiv({ cls: "davyjones-tm-section" });
    scopeSection.createEl("label", { text: "Scope", cls: "davyjones-tm-label" });
    const scopeRow = scopeSection.createDiv({ cls: "davyjones-tm-scope-row" });

    const scopeOptions = [
      { value: "file", label: "Current file" },
      { value: "folder", label: "Current folder" },
      { value: "vault", label: "Entire vault" },
    ];

    for (const opt of scopeOptions) {
      const btn = scopeRow.createEl("button", {
        text: opt.label,
        cls: `davyjones-tm-scope-btn${this._scope === opt.value ? " is-active" : ""}`,
      });
      btn.addEventListener("click", () => {
        this._scope = opt.value;
        scopeRow.querySelectorAll(".davyjones-tm-scope-btn").forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");
      });
    }

    // Advanced options (collapsible)
    const advToggle = contentEl.createDiv({ cls: "davyjones-tm-adv-toggle" });
    advToggle.createEl("span", { text: "\u25B8 Advanced options" });
    const advContainer = contentEl.createDiv({ cls: "davyjones-tm-advanced" });
    advContainer.style.display = "none";

    advToggle.addEventListener("click", () => {
      const visible = advContainer.style.display !== "none";
      advContainer.style.display = visible ? "none" : "block";
      advToggle.querySelector("span").textContent = (visible ? "\u25B8" : "\u25BE") + " Advanced options";
    });

    new Setting(advContainer).setName("Max turns per agent").addText((text) => {
      text.setPlaceholder("20").setValue(String(this._maxTurns));
      text.onChange((v) => { this._maxTurns = parseInt(v, 10) || 20; });
      text.inputEl.type = "number";
      text.inputEl.style.width = "70px";
    });

    new Setting(advContainer).setName("Verbosity").addDropdown((drop) => {
      drop.addOption("concise", "Concise")
        .addOption("normal", "Normal")
        .addOption("detailed", "Detailed")
        .setValue(this._verbosity)
        .onChange((v) => { this._verbosity = v; });
    });

    // Footer with send button
    const footer = contentEl.createDiv({ cls: "davyjones-tm-footer" });
    const sendBtn = footer.createEl("button", { text: "Send Task", cls: "davyjones-tm-send-btn" });
    sendBtn.addEventListener("click", () => this._submit());
  }

  async _submit() {
    const description = this._textarea.value.trim();
    if (!description) {
      new Notice("Task description is required.");
      return;
    }

    const scopeFiles = this._resolveScopeFiles();
    const env = this.plugin._readDavyJonesEnv();
    const port = env.HTTP_PORT || "5555";
    const url = `http://localhost:${port}/api/task`;

    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          scope: this._scope,
          scopeFiles,
          maxTurns: this._maxTurns,
          verbosity: this._verbosity,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        new Notice("Task failed: " + (err.error || resp.statusText));
        return;
      }

      const result = await resp.json();
      new Notice(`Task sent (${result.task_id}). Agents will handle it.`);
      this.close();
    } catch (e) {
      new Notice("Could not reach dispatcher. Is DavyJones running?");
      console.error("DavyJones task submit error:", e);
    }
  }

  _resolveScopeFiles() {
    const activeFile = this.app.workspace.getActiveFile();

    if (this._scope === "file" && activeFile) {
      return [activeFile.path];
    }

    if (this._scope === "folder" && activeFile && activeFile.parent) {
      const parentPath = activeFile.parent.path;
      return this.app.vault.getMarkdownFiles()
        .filter((f) => f.parent && f.parent.path === parentPath)
        .map((f) => f.path);
    }

    // "vault" scope — empty list, agents discover at runtime
    return [];
  }

  onClose() {
    this.contentEl.empty();
  }
}

// ─── Reports View ────────────────────────────────────────────

class DavyJonesReportsView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this._reports = [];
    this._offset = 0;
    this._expandedReports = new Set();
    this._expandedTasks = new Set();
    this._reportCache = {};
    this._lastFetchCount = 0;
  }

  getViewType() { return REPORTS_VIEW_TYPE; }
  getDisplayText() { return "Agent Reports"; }
  getIcon() { return "file-text"; }

  async onOpen() {
    this.contentEl.empty();
    this.contentEl.addClass("davyjones-reports-root");
    this._loadReports(true);
  }

  async onClose() { this.contentEl.empty(); }

  refresh() { this._checkForNew(); }

  _apiBase() {
    const env = this.plugin._readDavyJonesEnv();
    const port = env.HTTP_PORT || "5555";
    return `http://localhost:${port}`;
  }

  async _checkForNew() {
    try {
      const resp = await fetch(`${this._apiBase()}/api/reports?limit=1&offset=0`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.reports.length > 0 && (this._reports.length === 0 || data.reports[0].id !== this._reports[0].id)) {
        this._loadReports(true);
      }
    } catch { /* dispatcher offline */ }
  }

  async _loadReports(reset) {
    if (reset) {
      this._offset = 0;
      this._reports = [];
      this._expandedReports.clear();
      this._expandedTasks.clear();
    }
    try {
      const resp = await fetch(`${this._apiBase()}/api/reports?limit=${REPORTS_PAGE_SIZE}&offset=${this._offset}`);
      if (!resp.ok) { this._renderReports(); return; }
      const data = await resp.json();
      this._reports = this._reports.concat(data.reports);
      this._lastFetchCount = data.reports.length;
      this._offset += data.reports.length;
    } catch { /* dispatcher offline */ }
    this._renderReports();
  }

  async _loadReportDetail(reportId) {
    if (this._reportCache[reportId]) return this._reportCache[reportId];
    try {
      const resp = await fetch(`${this._apiBase()}/api/reports/${reportId}`);
      if (!resp.ok) return null;
      const detail = await resp.json();
      this._reportCache[reportId] = detail;
      return detail;
    } catch { return null; }
  }

  _renderReports() {
    this.contentEl.empty();

    const header = this.contentEl.createDiv({ cls: "davyjones-reports-header" });
    header.createEl("h4", { text: "Agent Reports" });

    if (this._reports.length === 0) {
      this.contentEl.createEl("p", {
        text: "No reports yet. Reports are generated after each agent execution.",
        cls: "davyjones-reports-empty",
      });
      return;
    }

    const list = this.contentEl.createDiv({ cls: "davyjones-reports-list" });

    for (const report of this._reports) {
      const item = list.createDiv({ cls: "davyjones-reports-card" });
      const row = item.createDiv({ cls: "davyjones-reports-card-row" });

      const isExpanded = this._expandedReports.has(report.id);
      row.createEl("span", { cls: "davyjones-reports-toggle", text: isExpanded ? "\u25BE" : "\u25B8" });
      row.createEl("span", { cls: `davyjones-prop-badge davyjones-s-${report.status}`, text: report.status });

      const desc = report.description.length > 60 ? report.description.slice(0, 60) + "..." : report.description;
      row.createEl("span", { cls: "davyjones-reports-desc", text: desc });

      const taskLabel = report.failed > 0
        ? `${report.succeeded}/${report.task_count} ok`
        : `${report.task_count} tasks`;
      row.createEl("span", { cls: "davyjones-reports-task-count", text: taskLabel });
      row.createEl("span", { cls: "davyjones-reports-date", text: this._relativeDate(report.created_at) });

      row.addEventListener("click", async () => {
        if (this._expandedReports.has(report.id)) {
          this._expandedReports.delete(report.id);
          this._renderReports();
        } else {
          await this._loadReportDetail(report.id);
          this._expandedReports.add(report.id);
          this._renderReports();
        }
      });

      if (isExpanded) {
        const detail = this._reportCache[report.id];
        if (detail) {
          const detailDiv = item.createDiv({ cls: "davyjones-reports-detail" });

          // Scribe's prose summary
          if (detail.summary) {
            detailDiv.createEl("div", { cls: "davyjones-reports-summary", text: detail.summary });
          }

          detailDiv.createEl("div", {
            cls: "davyjones-reports-meta",
            text: `Duration: ${detail.duration_seconds.toFixed(1)}s | Source: ${detail.source}`,
          });

          if (detail.tasks) {
            const tasksDiv = detailDiv.createDiv({ cls: "davyjones-reports-tasks" });
            for (const task of detail.tasks) {
              const taskKey = `${report.id}:${task.id}`;
              const taskExpanded = this._expandedTasks.has(taskKey);
              const taskRow = tasksDiv.createDiv({ cls: "davyjones-reports-task-row" });

              taskRow.createEl("span", { cls: "davyjones-reports-toggle", text: taskExpanded ? "\u25BE" : "\u25B8" });
              taskRow.createEl("span", { cls: `davyjones-prop-badge davyjones-s-${task.status}`, text: task.status });
              taskRow.createEl("span", { cls: "davyjones-reports-task-desc", text: `${task.id}: ${task.description}` });
              if (task.hit_max_turns) {
                taskRow.createEl("span", { cls: "davyjones-reports-warn", text: "max turns" });
              }

              taskRow.addEventListener("click", (e) => {
                e.stopPropagation();
                if (this._expandedTasks.has(taskKey)) {
                  this._expandedTasks.delete(taskKey);
                } else {
                  this._expandedTasks.add(taskKey);
                }
                this._renderReports();
              });

              if (taskExpanded) {
                const outputText = task.error ? `Error: ${task.error}` : task.summary || "(no output)";
                tasksDiv.createEl("pre", { cls: "davyjones-reports-output", text: outputText });
              }
            }
          }
        }
      }
    }

    if (this._lastFetchCount >= REPORTS_PAGE_SIZE) {
      const moreBtn = this.contentEl.createEl("button", { cls: "davyjones-reports-more-btn", text: "Load more" });
      moreBtn.addEventListener("click", () => this._loadReports(false));
    }
  }

  _relativeDate(dateStr) {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(dateStr).toLocaleDateString();
  }
}

// ─── History View ─────────────────────────────────────────────

class DavyJonesHistoryView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this._commits = [];
    this._offset = 0;
    this._expandedCommits = new Set();
    this._expandedFiles = {};
    this._lastKnownHead = null;
  }

  getViewType() { return HISTORY_VIEW_TYPE; }
  getDisplayText() { return "Vault History"; }
  getIcon() { return "git-branch"; }

  async onOpen() {
    this.contentEl.empty();
    this.contentEl.addClass("davyjones-history-root");
    this._loadCommits(true);
  }

  async onClose() { this.contentEl.empty(); }

  refresh() {
    try {
      const head = execSync("git rev-parse HEAD", {
        cwd: this.plugin._vaultPath, encoding: "utf8", timeout: 3000,
      }).trim();
      if (head === this._lastKnownHead) return;
      this._lastKnownHead = head;
    } catch { /* ignore */ }
    this._loadCommits(true);
  }

  _loadCommits(reset) {
    if (reset) {
      this._offset = 0;
      this._commits = [];
      this._expandedCommits.clear();
      this._expandedFiles = {};
    }
    try {
      const sep = "---DJ-COMMIT---";
      const raw = execSync(
        `git log --pretty=format:"${sep}%n%H%n%h%n%s%n%ai" --name-only --skip=${this._offset} -n ${HISTORY_PAGE_SIZE}`,
        { cwd: this.plugin._vaultPath, encoding: "utf8", timeout: 10000 }
      ).trim();
      if (!raw) { this._renderCommits(); return; }

      const blocks = raw.split(sep).filter(b => b.trim());
      for (const block of blocks) {
        const lines = block.trim().split("\n");
        if (lines.length < 4) continue;
        const [fullHash, shortHash, message, date, ...fileLines] = lines;
        const files = fileLines.filter(f => f.trim());
        this._commits.push({ fullHash, shortHash, message, date, files });
      }
      this._offset += blocks.length;

      if (reset && this._commits.length > 0) {
        this._lastKnownHead = this._commits[0].fullHash;
      }
    } catch (e) {
      console.error("DavyJones: git log failed", e);
    }
    this._renderCommits();
  }

  _renderCommits() {
    this.contentEl.empty();

    const header = this.contentEl.createDiv({ cls: "davyjones-history-header" });
    header.createEl("h4", { text: "Vault History" });

    if (this._commits.length === 0) {
      this.contentEl.createEl("p", { text: "No commits yet.", cls: "davyjones-history-empty" });
      return;
    }

    const list = this.contentEl.createDiv({ cls: "davyjones-history-list" });

    for (const commit of this._commits) {
      const item = list.createDiv({ cls: "davyjones-history-commit" });
      const row = item.createDiv({ cls: "davyjones-history-commit-row" });

      const isExpanded = this._expandedCommits.has(commit.fullHash);
      row.createEl("span", { cls: "davyjones-history-toggle", text: isExpanded ? "\u25BE" : "\u25B8" });
      row.createEl("span", { cls: "davyjones-history-hash", text: commit.shortHash });
      row.createEl("span", { cls: "davyjones-history-msg", text: commit.message });
      row.createEl("span", { cls: "davyjones-history-date", text: this._relativeDate(commit.date) });
      row.createEl("span", {
        cls: "davyjones-history-file-count",
        text: `${commit.files.length} file${commit.files.length !== 1 ? "s" : ""}`,
      });

      const revertBtn = row.createEl("button", { cls: "davyjones-history-revert-btn", text: "Revert" });
      revertBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this._revertToCommit(commit.fullHash, commit.shortHash);
      });

      row.addEventListener("click", () => {
        if (this._expandedCommits.has(commit.fullHash)) {
          this._expandedCommits.delete(commit.fullHash);
        } else {
          this._expandedCommits.add(commit.fullHash);
        }
        this._renderCommits();
      });

      if (isExpanded && commit.files.length > 0) {
        const filesDiv = item.createDiv({ cls: "davyjones-history-files" });
        for (const file of commit.files) {
          const diffKey = `${commit.fullHash}:${file}`;
          const fileRow = filesDiv.createDiv({ cls: "davyjones-history-file" });
          fileRow.createEl("span", { cls: "davyjones-history-filename", text: file });

          fileRow.addEventListener("click", (e) => {
            e.stopPropagation();
            if (this._expandedFiles[diffKey]) {
              delete this._expandedFiles[diffKey];
            } else {
              try {
                const diff = execSync(
                  `git diff ${commit.fullHash}~1 ${commit.fullHash} -- "${file}"`,
                  { cwd: this.plugin._vaultPath, encoding: "utf8", timeout: 5000 }
                ).trim();
                this._expandedFiles[diffKey] = diff || "(no diff available)";
              } catch {
                this._expandedFiles[diffKey] = "(could not load diff)";
              }
            }
            this._renderCommits();
          });

          if (this._expandedFiles[diffKey]) {
            filesDiv.createEl("pre", { cls: "davyjones-history-diff", text: this._expandedFiles[diffKey] });
          }
        }
      }
    }

    if (this._commits.length >= this._offset) {
      const moreBtn = this.contentEl.createEl("button", { cls: "davyjones-history-more-btn", text: "Load more" });
      moreBtn.addEventListener("click", () => this._loadCommits(false));
    }
  }

  _revertToCommit(hash, shortHash) {
    if (!confirm(`Revert vault to commit ${shortHash}?\n\nThis checks out all files from that commit. Your current changes will appear as uncommitted modifications.`)) return;
    try {
      execSync(`git checkout ${hash} -- .`, { cwd: this.plugin._vaultPath, timeout: 15000 });
      new Notice("Vault reverted to " + shortHash);
      this.plugin._updateGitStatus();
      this._loadCommits(true);
    } catch (e) {
      new Notice("Revert failed: " + e.message);
    }
  }

  _relativeDate(dateStr) {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(dateStr).toLocaleDateString();
  }
}

// ─── Calendar View ────────────────────────────────────────────

function _expandRecurrence(event, rangeStart, rangeEnd) {
  const rec = event.recurrence;
  if (!rec) return [];
  const base = new Date(event.start);
  const baseEnd = new Date(event.end || event.start);
  const dur = baseEnd.getTime() - base.getTime();
  const freq = rec.freq;
  const interval = rec.interval || 1;
  const until = rec.until ? new Date(rec.until) : null;
  const maxCount = rec.count || 1000;
  const byDay = (rec.byDay || []).map(d => ["SU","MO","TU","WE","TH","FR","SA"].indexOf(d));
  const occurrences = [];
  let cursor = new Date(base);
  let count = 0;

  for (let i = 0; i < 5000 && count < maxCount; i++) {
    if (until && cursor > until) break;
    if (cursor > rangeEnd) break;

    let matches = true;
    if (freq === "weekly" && byDay.length > 0) {
      matches = byDay.includes(cursor.getDay());
    }

    if (matches) {
      const occEnd = new Date(cursor.getTime() + dur);
      if (occEnd >= rangeStart && cursor <= rangeEnd) {
        occurrences.push({ start: new Date(cursor), end: occEnd, event });
      }
      if (freq !== "weekly" || byDay.length === 0) count++;
    }

    if (freq === "daily") {
      cursor.setDate(cursor.getDate() + (matches && (byDay.length === 0) ? interval : 1));
    } else if (freq === "weekly") {
      if (byDay.length > 0) {
        cursor.setDate(cursor.getDate() + 1);
        // When we pass Saturday, add interval-1 extra weeks
        if (cursor.getDay() === 0 && i > 0) cursor.setDate(cursor.getDate() + (interval - 1) * 7);
      } else {
        cursor.setDate(cursor.getDate() + interval * 7);
      }
    } else if (freq === "monthly") {
      cursor.setMonth(cursor.getMonth() + interval);
    } else if (freq === "yearly") {
      cursor.setFullYear(cursor.getFullYear() + interval);
    } else {
      break;
    }
  }
  return occurrences;
}

function _getEventsForRange(calData, rangeStart, rangeEnd) {
  const results = [];
  const calendars = {};
  for (const cal of (calData.calendars || [])) calendars[cal.id] = cal;

  for (const evt of (calData.events || [])) {
    const cal = calendars[evt.calendarId] || calData.calendars[0];
    const color = evt.color || (cal && cal.color) || "#7c3aed";

    if (evt.recurrence) {
      for (const occ of _expandRecurrence(evt, rangeStart, rangeEnd)) {
        results.push({ ...evt, _start: occ.start, _end: occ.end, _color: color });
      }
    } else {
      const s = new Date(evt.start);
      const e = new Date(evt.end || evt.start);
      if (e >= rangeStart && s <= rangeEnd) {
        results.push({ ...evt, _start: s, _end: e, _color: color });
      }
    }
  }
  return results;
}

function _formatTime(date) {
  const h = date.getHours();
  const m = date.getMinutes();
  const ampm = h >= 12 ? "pm" : "am";
  const h12 = h % 12 || 12;
  return m === 0 ? `${h12}${ampm}` : `${h12}:${String(m).padStart(2, "0")}${ampm}`;
}

const DAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

class DavyJonesCalendarView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this._viewMode = "month";
    this._currentDate = new Date();
    this._calendarData = null;
    this._selectedDate = null;
  }

  getViewType() { return CALENDAR_VIEW_TYPE; }
  getDisplayText() { return "Calendar"; }
  getIcon() { return "calendar"; }

  async onOpen() {
    this._calendarData = this.plugin._readCalendar();
    this._render();
  }

  async onClose() { this.contentEl.empty(); }

  refresh() {
    const fresh = this.plugin._readCalendar();
    if (JSON.stringify(fresh) !== JSON.stringify(this._calendarData)) {
      this._calendarData = fresh;
      this._render();
    }
  }

  _render() {
    const el = this.contentEl;
    el.empty();
    el.addClass("davyjones-cal-root");

    this._renderHeader(el);

    if (this._viewMode === "month") this._renderMonth(el);
    else if (this._viewMode === "week") this._renderWeek(el);
    else this._renderDay(el);
  }

  _renderHeader(container) {
    const toolbar = container.createDiv({ cls: "davyjones-cal-toolbar" });
    const d = this._currentDate;

    // Nav arrows
    const prevBtn = toolbar.createEl("button", { cls: "davyjones-cal-nav-btn", text: "<" });
    prevBtn.addEventListener("click", () => { this._navigate(-1); });
    const todayBtn = toolbar.createEl("button", { cls: "davyjones-cal-today-btn", text: "Today" });
    todayBtn.addEventListener("click", () => { this._currentDate = new Date(); this._selectedDate = null; this._render(); });
    const nextBtn = toolbar.createEl("button", { cls: "davyjones-cal-nav-btn", text: ">" });
    nextBtn.addEventListener("click", () => { this._navigate(1); });

    // Title
    let titleText = "";
    if (this._viewMode === "month") {
      titleText = `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    } else if (this._viewMode === "week") {
      const weekStart = this._getWeekStart(d);
      const weekEnd = new Date(weekStart); weekEnd.setDate(weekEnd.getDate() + 6);
      titleText = `${MONTHS[weekStart.getMonth()].slice(0,3)} ${weekStart.getDate()} – ${weekEnd.getDate()}, ${weekEnd.getFullYear()}`;
    } else {
      titleText = `${DAYS_SHORT[d.getDay()]}, ${MONTHS[d.getMonth()].slice(0,3)} ${d.getDate()}, ${d.getFullYear()}`;
    }
    toolbar.createEl("span", { cls: "davyjones-cal-title", text: titleText });

    // View toggle
    const toggle = toolbar.createDiv({ cls: "davyjones-cal-view-toggle" });
    for (const mode of ["month", "week", "day"]) {
      const btn = toggle.createEl("button", { cls: `davyjones-cal-view-btn ${this._viewMode === mode ? "is-active" : ""}`, text: mode.charAt(0).toUpperCase() + mode.slice(1) });
      btn.addEventListener("click", () => { this._viewMode = mode; this._render(); });
    }

    // Import button
    const importBtn = toolbar.createEl("button", { cls: "davyjones-cal-today-btn", text: "Import" });
    importBtn.addEventListener("click", () => { new DavyJonesICSImportModal(this.app, this.plugin).open(); });

    // New event button
    const newBtn = toolbar.createEl("button", { cls: "davyjones-cal-new-btn", text: "+ Event" });
    newBtn.addEventListener("click", () => {
      new DavyJonesEventModal(this.app, this.plugin, null, this._currentDate).open();
    });
  }

  _navigate(dir) {
    const d = this._currentDate;
    if (this._viewMode === "month") d.setMonth(d.getMonth() + dir);
    else if (this._viewMode === "week") d.setDate(d.getDate() + dir * 7);
    else d.setDate(d.getDate() + dir);
    this._selectedDate = null;
    this._render();
  }

  _getWeekStart(d) {
    const ws = new Date(d);
    ws.setDate(ws.getDate() - ws.getDay());
    ws.setHours(0, 0, 0, 0);
    return ws;
  }

  _isSameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  // ── Month View ──

  _renderMonth(container) {
    const scroll = container.createDiv({ cls: "davyjones-cal-month-scroll" });
    const grid = scroll.createDiv({ cls: "davyjones-cal-month-grid" });
    const today = new Date();
    const year = this._currentDate.getFullYear();
    const month = this._currentDate.getMonth();

    // Day-of-week headers
    for (const d of DAYS_SHORT) {
      grid.createDiv({ cls: "davyjones-cal-dow-header", text: d });
    }

    // Compute grid start (first visible Sunday)
    const firstOfMonth = new Date(year, month, 1);
    const gridStart = new Date(firstOfMonth);
    gridStart.setDate(gridStart.getDate() - gridStart.getDay());

    // Grid range
    const gridEnd = new Date(gridStart);
    gridEnd.setDate(gridEnd.getDate() + 42);

    const events = _getEventsForRange(this._calendarData, gridStart, gridEnd);

    for (let i = 0; i < 42; i++) {
      const cellDate = new Date(gridStart);
      cellDate.setDate(gridStart.getDate() + i);
      const isOtherMonth = cellDate.getMonth() !== month;
      const isToday = this._isSameDay(cellDate, today);
      const isSelected = this._selectedDate && this._isSameDay(cellDate, this._selectedDate);

      const cell = grid.createDiv({
        cls: `davyjones-cal-day-cell${isOtherMonth ? " davyjones-cal-other-month" : ""}${isToday ? " davyjones-cal-today" : ""}${isSelected ? " davyjones-cal-selected" : ""}`,
      });

      const numEl = cell.createDiv({ cls: "davyjones-cal-day-num" });
      numEl.createEl("span", { text: String(cellDate.getDate()) });

      // Events for this day
      const dayStart = new Date(cellDate); dayStart.setHours(0, 0, 0, 0);
      const dayEnd = new Date(cellDate); dayEnd.setHours(23, 59, 59, 999);
      const dayEvents = events.filter(e => e._start <= dayEnd && e._end >= dayStart);

      const eventsEl = cell.createDiv({ cls: "davyjones-cal-day-events" });
      const maxShow = 3;
      for (let j = 0; j < Math.min(dayEvents.length, maxShow); j++) {
        const ev = dayEvents[j];
        const chip = eventsEl.createDiv({ cls: `davyjones-cal-event-chip${ev.type === "task" ? " is-task" : ""}` });
        chip.style.background = ev._color;
        chip.setText(ev.allDay ? ev.title : `${_formatTime(ev._start)} ${ev.title}`);
        chip.addEventListener("click", (e) => {
          e.stopPropagation();
          const original = (this._calendarData.events || []).find(x => x.id === ev.id);
          if (original) new DavyJonesEventModal(this.app, this.plugin, original).open();
        });
      }
      if (dayEvents.length > maxShow) {
        eventsEl.createDiv({ cls: "davyjones-cal-overflow", text: `+${dayEvents.length - maxShow} more` });
      }

      cell.addEventListener("click", () => {
        this._selectedDate = new Date(cellDate);
        this._render();
      });
    }

    // Day detail panel
    if (this._selectedDate) {
      this._renderDayDetail(container);
    }
  }

  _renderDayDetail(container) {
    const detail = container.createDiv({ cls: "davyjones-cal-day-detail" });
    const d = this._selectedDate;
    const dayStart = new Date(d); dayStart.setHours(0, 0, 0, 0);
    const dayEnd = new Date(d); dayEnd.setHours(23, 59, 59, 999);
    const events = _getEventsForRange(this._calendarData, dayStart, dayEnd);

    const headerRow = detail.createDiv({ cls: "davyjones-cal-detail-header" });
    headerRow.createEl("h4", { text: `${DAYS_SHORT[d.getDay()]}, ${MONTHS[d.getMonth()].slice(0,3)} ${d.getDate()}` });
    const addBtn = headerRow.createEl("button", { cls: "davyjones-cal-new-btn", text: "+ Add" });
    addBtn.addEventListener("click", () => {
      new DavyJonesEventModal(this.app, this.plugin, null, d).open();
    });

    if (events.length === 0) {
      detail.createEl("p", { cls: "davyjones-cal-detail-empty", text: "No events" });
      return;
    }

    events.sort((a, b) => a._start - b._start);
    for (const ev of events) {
      const row = detail.createDiv({ cls: "davyjones-cal-detail-event" });
      const dot = row.createEl("span", { cls: "davyjones-cal-detail-dot" });
      dot.style.background = ev._color;
      row.createEl("span", { cls: "davyjones-cal-detail-time", text: ev.allDay ? "All day" : _formatTime(ev._start) });
      row.createEl("span", { cls: "davyjones-cal-detail-title", text: ev.title });
      if (ev.type === "task") {
        const original = (this._calendarData.events || []).find(x => x.id === ev.id);
        const taskData = original?.task;
        const lastStatus = taskData?.lastStatus;
        const lastRun = taskData?.lastDispatchedAt;
        if (lastStatus) {
          const statusCls = lastStatus === "completed" ? "is-completed" : lastStatus === "failed" ? "is-failed" : "is-pending";
          row.createEl("span", { cls: `davyjones-cal-detail-badge ${statusCls}`, text: lastStatus });
        } else if (lastRun) {
          row.createEl("span", { cls: "davyjones-cal-detail-badge is-running", text: "running" });
        } else {
          row.createEl("span", { cls: "davyjones-cal-detail-badge", text: "task" });
        }
      }
      row.addEventListener("click", () => {
        const original = (this._calendarData.events || []).find(x => x.id === ev.id);
        if (original) new DavyJonesEventModal(this.app, this.plugin, original).open();
      });
    }
  }

  // ── Week View ──

  _renderWeek(container) {
    const scroll = container.createDiv({ cls: "davyjones-cal-week-scroll" });
    const weekStart = this._getWeekStart(this._currentDate);
    const weekEnd = new Date(weekStart); weekEnd.setDate(weekEnd.getDate() + 7);
    const today = new Date();
    const events = _getEventsForRange(this._calendarData, weekStart, weekEnd);

    // Header row
    const header = scroll.createDiv({ cls: "davyjones-cal-week-header" });
    header.createDiv({ cls: "davyjones-cal-gutter-spacer" }); // spacer for time gutter
    for (let i = 0; i < 7; i++) {
      const d = new Date(weekStart); d.setDate(d.getDate() + i);
      const hdr = header.createDiv({ cls: `davyjones-cal-week-day-header${this._isSameDay(d, today) ? " davyjones-cal-today" : ""}` });
      hdr.createEl("span", { text: `${DAYS_SHORT[d.getDay()]} ${d.getDate()}` });
    }

    // All-day row
    const allDayEvents = events.filter(e => e.allDay);
    if (allDayEvents.length > 0) {
      const allDayRow = scroll.createDiv({ cls: "davyjones-cal-allday-row" });
      allDayRow.createDiv({ cls: "davyjones-cal-allday-label", text: "all-day" });
      for (let i = 0; i < 7; i++) {
        const d = new Date(weekStart); d.setDate(d.getDate() + i);
        const ds = new Date(d); ds.setHours(0,0,0,0);
        const de = new Date(d); de.setHours(23,59,59,999);
        const cell = allDayRow.createDiv({ cls: "davyjones-cal-allday-cell" });
        for (const ev of allDayEvents.filter(e => e._start <= de && e._end >= ds)) {
          const chip = cell.createDiv({ cls: "davyjones-cal-event-chip" });
          chip.style.background = ev._color;
          chip.setText(ev.title);
          chip.addEventListener("click", (e) => {
            e.stopPropagation();
            const original = (this._calendarData.events || []).find(x => x.id === ev.id);
            if (original) new DavyJonesEventModal(this.app, this.plugin, original).open();
          });
        }
      }
    }

    // Time grid
    const grid = scroll.createDiv({ cls: "davyjones-cal-time-grid" });
    const timedEvents = events.filter(e => !e.allDay);

    // Time gutter
    const gutter = grid.createDiv({ cls: "davyjones-cal-time-gutter" });
    for (let h = 0; h < 24; h++) {
      const label = h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`;
      gutter.createDiv({ cls: "davyjones-cal-hour-label", text: label });
    }

    // Day columns
    for (let i = 0; i < 7; i++) {
      const d = new Date(weekStart); d.setDate(d.getDate() + i);
      const col = grid.createDiv({ cls: `davyjones-cal-week-col${this._isSameDay(d, today) ? " davyjones-cal-today-col" : ""}` });

      // Hour slots
      for (let h = 0; h < 24; h++) {
        const slot = col.createDiv({ cls: "davyjones-cal-hour-slot" });
        slot.addEventListener("click", () => {
          const clickDate = new Date(d);
          clickDate.setHours(h, 0, 0, 0);
          new DavyJonesEventModal(this.app, this.plugin, null, clickDate).open();
        });
      }

      // Event blocks
      const ds = new Date(d); ds.setHours(0,0,0,0);
      const de = new Date(d); de.setHours(23,59,59,999);
      const dayEvents = timedEvents.filter(e => e._start <= de && e._end >= ds);
      for (const ev of dayEvents) {
        const startH = ev._start < ds ? 0 : ev._start.getHours() + ev._start.getMinutes() / 60;
        const endH = ev._end > de ? 24 : ev._end.getHours() + ev._end.getMinutes() / 60;
        const top = startH * 60;
        const height = Math.max((endH - startH) * 60, 18);

        const block = col.createDiv({ cls: `davyjones-cal-event-block${ev.type === "task" ? " is-task" : ""}` });
        block.style.top = `${top}px`;
        block.style.height = `${height}px`;
        block.style.background = ev._color;
        block.createDiv({ cls: "davyjones-cal-event-block-title", text: ev.title });
        if (height > 30) block.createDiv({ cls: "davyjones-cal-event-block-time", text: `${_formatTime(ev._start)} – ${_formatTime(ev._end)}` });
        block.addEventListener("click", (e) => {
          e.stopPropagation();
          const original = (this._calendarData.events || []).find(x => x.id === ev.id);
          if (original) new DavyJonesEventModal(this.app, this.plugin, original).open();
        });
      }

      // Now line
      if (this._isSameDay(d, today)) {
        const nowH = today.getHours() + today.getMinutes() / 60;
        const nowLine = col.createDiv({ cls: "davyjones-cal-now-line" });
        nowLine.style.top = `${nowH * 60}px`;
        nowLine.createDiv({ cls: "davyjones-cal-now-dot" });
      }
    }
  }

  // ── Day View ──

  _renderDay(container) {
    const scroll = container.createDiv({ cls: "davyjones-cal-week-scroll" });
    const d = new Date(this._currentDate);
    d.setHours(0, 0, 0, 0);
    const dayEnd = new Date(d); dayEnd.setHours(23, 59, 59, 999);
    const today = new Date();
    const events = _getEventsForRange(this._calendarData, d, dayEnd);

    // Header
    const header = scroll.createDiv({ cls: "davyjones-cal-day-view-header" });
    header.createDiv({ cls: "davyjones-cal-gutter-spacer" });
    const hdr = header.createDiv({ cls: `davyjones-cal-week-day-header${this._isSameDay(d, today) ? " davyjones-cal-today" : ""}` });
    hdr.createEl("span", { text: `${DAYS_SHORT[d.getDay()]} ${d.getDate()}` });

    // All-day events
    const allDayEvents = events.filter(e => e.allDay);
    if (allDayEvents.length > 0) {
      const allDayRow = scroll.createDiv({ cls: "davyjones-cal-allday-row davyjones-cal-day-allday" });
      allDayRow.createDiv({ cls: "davyjones-cal-allday-label", text: "all-day" });
      const cell = allDayRow.createDiv({ cls: "davyjones-cal-allday-cell" });
      for (const ev of allDayEvents) {
        const chip = cell.createDiv({ cls: "davyjones-cal-event-chip" });
        chip.style.background = ev._color;
        chip.setText(ev.title);
        chip.addEventListener("click", (e) => {
          e.stopPropagation();
          const original = (this._calendarData.events || []).find(x => x.id === ev.id);
          if (original) new DavyJonesEventModal(this.app, this.plugin, original).open();
        });
      }
    }

    // Time grid (single column)
    const grid = scroll.createDiv({ cls: "davyjones-cal-day-grid" });
    const timedEvents = events.filter(e => !e.allDay);

    const gutter = grid.createDiv({ cls: "davyjones-cal-time-gutter" });
    for (let h = 0; h < 24; h++) {
      const label = h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`;
      gutter.createDiv({ cls: "davyjones-cal-hour-label", text: label });
    }

    const col = grid.createDiv({ cls: `davyjones-cal-week-col${this._isSameDay(d, today) ? " davyjones-cal-today-col" : ""}` });

    for (let h = 0; h < 24; h++) {
      const slot = col.createDiv({ cls: "davyjones-cal-hour-slot" });
      slot.addEventListener("click", () => {
        const clickDate = new Date(d);
        clickDate.setHours(h, 0, 0, 0);
        new DavyJonesEventModal(this.app, this.plugin, null, clickDate).open();
      });
    }

    for (const ev of timedEvents) {
      const startH = ev._start.getHours() + ev._start.getMinutes() / 60;
      const endH = ev._end.getHours() + ev._end.getMinutes() / 60;
      const top = startH * 60;
      const height = Math.max((endH - startH) * 60, 18);

      const block = col.createDiv({ cls: `davyjones-cal-event-block${ev.type === "task" ? " is-task" : ""}` });
      block.style.top = `${top}px`;
      block.style.height = `${height}px`;
      block.style.background = ev._color;
      block.createDiv({ cls: "davyjones-cal-event-block-title", text: ev.title });
      if (height > 30) block.createDiv({ cls: "davyjones-cal-event-block-time", text: `${_formatTime(ev._start)} – ${_formatTime(ev._end)}` });
      block.addEventListener("click", (e) => {
        e.stopPropagation();
        const original = (this._calendarData.events || []).find(x => x.id === ev.id);
        if (original) new DavyJonesEventModal(this.app, this.plugin, original).open();
      });
    }

    if (this._isSameDay(d, today)) {
      const nowH = today.getHours() + today.getMinutes() / 60;
      const nowLine = col.createDiv({ cls: "davyjones-cal-now-line" });
      nowLine.style.top = `${nowH * 60}px`;
      nowLine.createDiv({ cls: "davyjones-cal-now-dot" });
    }
  }
}

// ─── Event Modal ──────────────────────────────────────────────

class DavyJonesEventModal extends Modal {
  constructor(app, plugin, existingEvent = null, defaultDate = null) {
    super(app);
    this.plugin = plugin;
    this._event = existingEvent;
    this._isEdit = !!existingEvent;

    const now = defaultDate || new Date();
    const defStart = existingEvent?.start || this._toLocalISO(now);
    const defEndDate = new Date(now); defEndDate.setHours(defEndDate.getHours() + 1);
    const defEnd = existingEvent?.end || this._toLocalISO(defEndDate);

    this._title = existingEvent?.title || "";
    this._description = existingEvent?.description || "";
    this._start = defStart;
    this._end = defEnd;
    this._allDay = existingEvent?.allDay || false;
    this._color = existingEvent?.color || null;
    this._type = existingEvent?.type || "event";
    this._calendarId = existingEvent?.calendarId || "default";
    this._recurrence = existingEvent?.recurrence ? { ...existingEvent.recurrence } : null;
    this._task = existingEvent?.task ? { ...existingEvent.task } : { agentDescription: "", scopeFiles: [], maxTurns: 20 };
  }

  _toLocalISO(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  _toDateOnly(str) {
    return str ? str.slice(0, 10) : "";
  }

  _generateId() {
    const bytes = new Uint8Array(4);
    crypto.getRandomValues(bytes);
    return "evt-" + Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join("");
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass("davyjones-event-modal");
    this._renderForm();
  }

  onClose() { this.contentEl.empty(); }

  _renderForm() {
    const el = this.contentEl;
    el.empty();

    el.createEl("h2", { text: this._isEdit ? "Edit Event" : "New Event" });

    // Title
    const titleInput = el.createEl("input", { type: "text", cls: "davyjones-em-input", placeholder: "Event title..." });
    titleInput.value = this._title;
    titleInput.addEventListener("input", () => { this._title = titleInput.value; });

    // All-day toggle + date/time
    const dateSection = el.createDiv({ cls: "davyjones-em-date-section" });

    new Setting(dateSection).setName("All day").addToggle((t) => {
      t.setValue(this._allDay).onChange((v) => { this._allDay = v; this._renderForm(); });
    });

    if (this._allDay) {
      const startDate = dateSection.createEl("input", { type: "date", cls: "davyjones-em-date-input" });
      startDate.value = this._toDateOnly(this._start);
      startDate.addEventListener("change", () => { this._start = startDate.value; });

      dateSection.createEl("span", { text: "to", cls: "davyjones-em-date-sep" });

      const endDate = dateSection.createEl("input", { type: "date", cls: "davyjones-em-date-input" });
      endDate.value = this._toDateOnly(this._end);
      endDate.addEventListener("change", () => { this._end = endDate.value; });
    } else {
      const startInput = dateSection.createEl("input", { type: "datetime-local", cls: "davyjones-em-date-input" });
      startInput.value = this._start.slice(0, 16);
      startInput.addEventListener("change", () => { this._start = startInput.value; });

      dateSection.createEl("span", { text: "to", cls: "davyjones-em-date-sep" });

      const endInput = dateSection.createEl("input", { type: "datetime-local", cls: "davyjones-em-date-input" });
      endInput.value = this._end.slice(0, 16);
      endInput.addEventListener("change", () => { this._end = endInput.value; });
    }

    // Color picker
    const colorSection = el.createDiv({ cls: "davyjones-em-color-picker" });
    colorSection.createEl("span", { text: "Color:", cls: "davyjones-em-color-label" });
    // "default" swatch
    const defSwatch = colorSection.createDiv({ cls: `davyjones-em-color-swatch${this._color === null ? " is-selected" : ""}` });
    defSwatch.style.background = "var(--background-modifier-border)";
    defSwatch.title = "Calendar default";
    defSwatch.addEventListener("click", () => { this._color = null; this._renderForm(); });
    for (const c of CAL_COLORS) {
      const swatch = colorSection.createDiv({ cls: `davyjones-em-color-swatch${this._color === c ? " is-selected" : ""}` });
      swatch.style.background = c;
      swatch.addEventListener("click", () => { this._color = c; this._renderForm(); });
    }

    // Description
    el.createEl("label", { text: "Description", cls: "davyjones-tm-label" });
    const descArea = el.createEl("textarea", { cls: "davyjones-tm-textarea" });
    descArea.value = this._description;
    descArea.rows = 3;
    descArea.addEventListener("input", () => { this._description = descArea.value; });

    // Recurrence
    const recSection = el.createDiv({ cls: "davyjones-em-recurrence-section" });
    new Setting(recSection).setName("Repeat").addDropdown((d) => {
      d.addOption("none", "None").addOption("daily", "Daily").addOption("weekly", "Weekly")
        .addOption("monthly", "Monthly").addOption("yearly", "Yearly")
        .setValue(this._recurrence?.freq || "none")
        .onChange((v) => {
          if (v === "none") { this._recurrence = null; } else {
            this._recurrence = { freq: v, interval: this._recurrence?.interval || 1, byDay: this._recurrence?.byDay || [], until: null, count: null };
          }
          this._renderForm();
        });
    });

    if (this._recurrence) {
      const recDetail = recSection.createDiv({ cls: "davyjones-em-rec-detail" });
      new Setting(recDetail).setName("Every").addText((t) => {
        t.setValue(String(this._recurrence.interval || 1)).onChange((v) => { this._recurrence.interval = parseInt(v, 10) || 1; });
        t.inputEl.type = "number"; t.inputEl.style.width = "60px";
      });

      if (this._recurrence.freq === "weekly") {
        const dayRow = recDetail.createDiv({ cls: "davyjones-em-rec-days" });
        const dayNames = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];
        const labels = ["S", "M", "T", "W", "T", "F", "S"];
        for (let i = 0; i < 7; i++) {
          const isOn = (this._recurrence.byDay || []).includes(dayNames[i]);
          const dayBtn = dayRow.createEl("button", { cls: `davyjones-em-rec-day-btn${isOn ? " is-active" : ""}`, text: labels[i] });
          dayBtn.addEventListener("click", () => {
            if (!this._recurrence.byDay) this._recurrence.byDay = [];
            const idx = this._recurrence.byDay.indexOf(dayNames[i]);
            if (idx >= 0) this._recurrence.byDay.splice(idx, 1);
            else this._recurrence.byDay.push(dayNames[i]);
            this._renderForm();
          });
        }
      }
    }

    // Schedule as Agent Task
    const taskSection = el.createDiv({ cls: "davyjones-em-task-toggle" });
    new Setting(taskSection).setName("Schedule as Agent Task")
      .setDesc("DavyJones will dispatch this event as an agent task at the scheduled time.")
      .addToggle((t) => {
        t.setValue(this._type === "task").onChange((v) => {
          this._type = v ? "task" : "event";
          this._renderForm();
        });
      });

    if (this._type === "task") {
      const taskConfig = el.createDiv({ cls: "davyjones-em-task-section" });
      taskConfig.createEl("label", { text: "Agent Prompt", cls: "davyjones-tm-label" });
      const agentDesc = taskConfig.createEl("textarea", { cls: "davyjones-tm-textarea" });
      agentDesc.value = this._task.agentDescription || "";
      agentDesc.rows = 4;
      agentDesc.placeholder = "Describe what the agent should do...";
      agentDesc.addEventListener("input", () => { this._task.agentDescription = agentDesc.value; });

      new Setting(taskConfig).setName("Max Turns").addText((t) => {
        t.setValue(String(this._task.maxTurns || 20)).onChange((v) => { this._task.maxTurns = parseInt(v, 10) || 20; });
        t.inputEl.type = "number"; t.inputEl.style.width = "70px";
      });

      // Run now + Dispatch history (edit mode only)
      if (this._isEdit && this._event?.task) {
        const runNowBtn = taskConfig.createEl("button", { cls: "davyjones-em-run-now-btn", text: "Run now" });
        runNowBtn.addEventListener("click", () => this._runNow(runNowBtn));

        const historyEl = taskConfig.createDiv({ cls: "davyjones-em-dispatch-history" });
        const lastRun = this._event.task.lastDispatchedAt;
        const lastStatus = this._event.task.lastStatus;
        const lastError = this._event.task.lastError;
        const instances = this._event.task.dispatchedInstances || [];

        if (lastRun) {
          const ago = this._timeAgo(new Date(lastRun));
          const statusText = lastStatus === "completed" ? "✓ Completed" : lastStatus === "failed" ? "✗ Failed" : "⟳ Running";
          const statusCls = lastStatus === "completed" ? "is-completed" : lastStatus === "failed" ? "is-failed" : "is-running";
          historyEl.createDiv({ cls: `davyjones-em-dispatch-status ${statusCls}`, text: `Last run: ${statusText} (${ago})` });
          if (lastError) {
            historyEl.createDiv({ cls: "davyjones-em-dispatch-error", text: lastError });
          }
        }
        if (instances.length > 0) {
          historyEl.createDiv({ cls: "davyjones-em-dispatch-count", text: `${instances.length} dispatch${instances.length !== 1 ? "es" : ""} total` });
        }
      }
    }

    // Footer
    const footer = el.createDiv({ cls: "davyjones-mcp-modal-actions" });
    if (this._isEdit) {
      const delBtn = footer.createEl("button", { cls: "davyjones-em-delete-btn", text: "Delete" });
      delBtn.addEventListener("click", () => this._delete());
    }
    const saveBtn = footer.createEl("button", { cls: "davyjones-mcp-modal-save", text: "Save" });
    saveBtn.addEventListener("click", () => this._save());
    const cancelBtn = footer.createEl("button", { cls: "davyjones-mcp-modal-cancel", text: "Cancel" });
    cancelBtn.addEventListener("click", () => this.close());
  }

  _save() {
    if (!this._title.trim()) { new Notice("Title is required."); return; }
    if (!this._start) { new Notice("Start date is required."); return; }

    const calData = this.plugin._readCalendar();
    const built = this._buildEvent(this._isEdit ? this._event.id : this._generateId());

    if (this._isEdit) {
      const idx = calData.events.findIndex(e => e.id === this._event.id);
      if (idx >= 0) calData.events[idx] = built;
      else calData.events.push(built);
    } else {
      calData.events.push(built);
    }

    this.plugin._writeCalendar(calData);
    this.close();
    this._refreshCalendarViews();
  }

  _delete() {
    const calData = this.plugin._readCalendar();
    calData.events = calData.events.filter(e => e.id !== this._event.id);
    this.plugin._writeCalendar(calData);
    this.close();
    this._refreshCalendarViews();
  }

  _refreshCalendarViews() {
    for (const leaf of this.app.workspace.getLeavesOfType(CALENDAR_VIEW_TYPE)) {
      if (leaf.view && leaf.view.refresh) leaf.view.refresh();
    }
  }

  _timeAgo(date) {
    const secs = Math.floor((Date.now() - date.getTime()) / 1000);
    if (secs < 60) return "just now";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  async _runNow(btn) {
    const description = this._task.agentDescription?.trim();
    if (!description) {
      new Notice("Agent prompt is required to run a task.");
      return;
    }

    btn.disabled = true;
    btn.textContent = "Dispatching…";

    const env = this.plugin._readDavyJonesEnv();
    const port = env.HTTP_PORT || "5555";
    const url = `http://localhost:${port}/api/task`;

    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          scopeFiles: this._task.scopeFiles || [],
          maxTurns: this._task.maxTurns || 20,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        new Notice("Run failed: " + (err.error || resp.statusText));
        btn.disabled = false;
        btn.textContent = "Run now";
        return;
      }

      const result = await resp.json();
      new Notice(`Task dispatched (${result.task_id}). Agents will handle it.`);
      btn.textContent = "Dispatched";
    } catch (e) {
      new Notice("Run failed: " + e.message);
      btn.disabled = false;
      btn.textContent = "Run now";
    }
  }

  _buildEvent(id) {
    return {
      id,
      calendarId: this._calendarId,
      title: this._title.trim(),
      description: this._description,
      start: this._start,
      end: this._end || this._start,
      allDay: this._allDay,
      color: this._color,
      type: this._type,
      recurrence: this._recurrence,
      task: this._type === "task" ? {
        agentDescription: this._task.agentDescription,
        scopeFiles: this._task.scopeFiles || [],
        maxTurns: this._task.maxTurns || 20,
        lastDispatchedAt: this._event?.task?.lastDispatchedAt || null,
        dispatchedInstances: this._event?.task?.dispatchedInstances || [],
      } : null,
    };
  }
}

// ─── ICS Import Modal ─────────────────────────────────────────

class DavyJonesICSImportModal extends Modal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this._parsedEvents = [];
    this._calendarName = "";
    this._calendarColor = "#0891b2";
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass("davyjones-ics-modal");
    contentEl.createEl("h2", { text: "Import .ics Calendar" });

    const fileInput = contentEl.createEl("input", { type: "file", attr: { accept: ".ics,.ical" } });
    fileInput.style.display = "none";
    const selectBtn = contentEl.createEl("button", { cls: "davyjones-cal-new-btn", text: "Select .ics File" });
    selectBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => { if (fileInput.files[0]) this._handleFile(fileInput.files[0]); });

    const dropZone = contentEl.createDiv({ cls: "davyjones-ics-dropzone" });
    dropZone.createEl("span", { text: "or drag & drop .ics file here" });
    dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.addClass("is-dragover"); });
    dropZone.addEventListener("dragleave", () => dropZone.removeClass("is-dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault(); dropZone.removeClass("is-dragover");
      if (e.dataTransfer.files.length) this._handleFile(e.dataTransfer.files[0]);
    });

    this._previewEl = contentEl.createDiv({ cls: "davyjones-ics-preview" });
  }

  onClose() { this.contentEl.empty(); }

  async _handleFile(file) {
    const text = await file.text();
    this._calendarName = file.name.replace(/\.(ics|ical)$/i, "");
    this._parsedEvents = this._parseICS(text);
    this._renderPreview();
  }

  _parseICS(text) {
    // Unfold continuation lines
    const unfolded = text.replace(/\r\n[ \t]/g, "").replace(/\r/g, "");
    const lines = unfolded.split("\n");
    const events = [];
    let current = null;

    for (const line of lines) {
      if (line === "BEGIN:VEVENT") { current = {}; continue; }
      if (line === "END:VEVENT" && current) {
        if (current.title && current.start) {
          const bytes = new Uint8Array(4); crypto.getRandomValues(bytes);
          events.push({
            id: "evt-" + Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join(""),
            calendarId: "", // filled on import
            title: current.title,
            description: current.description || "",
            start: current.start,
            end: current.end || current.start,
            allDay: current.allDay || false,
            color: null,
            type: "event",
            recurrence: current.recurrence || null,
            task: null,
          });
        }
        current = null;
        continue;
      }
      if (!current) continue;

      const colonIdx = line.indexOf(":");
      if (colonIdx < 0) continue;
      const key = line.slice(0, colonIdx);
      const value = line.slice(colonIdx + 1).trim();
      const baseProp = key.split(";")[0];

      if (baseProp === "SUMMARY") current.title = value;
      else if (baseProp === "DESCRIPTION") current.description = value.replace(/\\n/g, "\n").replace(/\\,/g, ",");
      else if (baseProp === "DTSTART") {
        const { dt, allDay } = this._parseICSDate(key, value);
        current.start = dt; current.allDay = allDay;
      } else if (baseProp === "DTEND") {
        const { dt } = this._parseICSDate(key, value);
        current.end = dt;
      } else if (baseProp === "RRULE") {
        current.recurrence = this._parseRRule(value);
      }
    }
    return events;
  }

  _parseICSDate(key, value) {
    const isDateOnly = key.includes("VALUE=DATE") || value.length === 8;
    if (isDateOnly) {
      const y = value.slice(0, 4), m = value.slice(4, 6), d = value.slice(6, 8);
      return { dt: `${y}-${m}-${d}`, allDay: true };
    }
    // YYYYMMDDTHHMMSS or YYYYMMDDTHHMMSSZ
    const clean = value.replace("Z", "");
    const y = clean.slice(0, 4), mo = clean.slice(4, 6), d = clean.slice(6, 8);
    const h = clean.slice(9, 11), mi = clean.slice(11, 13);
    return { dt: `${y}-${mo}-${d}T${h}:${mi}`, allDay: false };
  }

  _parseRRule(value) {
    const parts = {};
    for (const pair of value.split(";")) {
      const [k, v] = pair.split("=");
      parts[k] = v;
    }
    const rec = { freq: (parts.FREQ || "daily").toLowerCase(), interval: parseInt(parts.INTERVAL || "1", 10), byDay: [], until: null, count: null };
    if (parts.BYDAY) rec.byDay = parts.BYDAY.split(",");
    if (parts.UNTIL) {
      const u = parts.UNTIL.replace("Z", "");
      rec.until = `${u.slice(0,4)}-${u.slice(4,6)}-${u.slice(6,8)}`;
    }
    if (parts.COUNT) rec.count = parseInt(parts.COUNT, 10);
    return rec;
  }

  _renderPreview() {
    const el = this._previewEl;
    el.empty();

    if (this._parsedEvents.length === 0) {
      el.createEl("p", { text: "No events found in file.", cls: "davyjones-cal-detail-empty" });
      return;
    }

    // Calendar name
    new Setting(el).setName("Calendar Name").addText((t) => {
      t.setValue(this._calendarName).onChange((v) => { this._calendarName = v; });
    });

    // Color
    const colorRow = el.createDiv({ cls: "davyjones-em-color-picker" });
    colorRow.createEl("span", { text: "Color:", cls: "davyjones-em-color-label" });
    for (const c of CAL_COLORS) {
      const swatch = colorRow.createDiv({ cls: `davyjones-em-color-swatch${this._calendarColor === c ? " is-selected" : ""}` });
      swatch.style.background = c;
      swatch.addEventListener("click", () => { this._calendarColor = c; this._renderPreview(); });
    }

    el.createEl("p", { cls: "davyjones-ics-count", text: `${this._parsedEvents.length} events found` });

    // Preview list
    const list = el.createDiv({ cls: "davyjones-ics-preview-list" });
    for (const evt of this._parsedEvents.slice(0, 50)) {
      const row = list.createDiv({ cls: "davyjones-ics-preview-item" });
      row.createEl("span", { cls: "davyjones-ics-preview-date", text: evt.start.slice(0, 10) });
      row.createEl("span", { cls: "davyjones-ics-preview-title", text: evt.title });
    }
    if (this._parsedEvents.length > 50) {
      list.createEl("p", { cls: "davyjones-cal-detail-empty", text: `...and ${this._parsedEvents.length - 50} more` });
    }

    // Import button
    const importBtn = el.createEl("button", { cls: "davyjones-cal-new-btn", text: `Import ${this._parsedEvents.length} events` });
    importBtn.style.marginTop = "12px";
    importBtn.addEventListener("click", () => this._import());
  }

  _import() {
    const calData = this.plugin._readCalendar();
    const bytes = new Uint8Array(4); crypto.getRandomValues(bytes);
    const calId = "imported-" + Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join("");

    calData.calendars.push({
      id: calId,
      name: this._calendarName || "Imported",
      color: this._calendarColor,
      source: "ics",
      importedAt: new Date().toISOString(),
    });

    for (const evt of this._parsedEvents) {
      evt.calendarId = calId;
      calData.events.push(evt);
    }

    this.plugin._writeCalendar(calData);
    this.close();
    new Notice(`Imported ${this._parsedEvents.length} events into "${this._calendarName}"`);

    for (const leaf of this.app.workspace.getLeavesOfType(CALENDAR_VIEW_TYPE)) {
      if (leaf.view && leaf.view.refresh) leaf.view.refresh();
    }
  }
}

// ─── Control Panel (center view) ──────────────────────────────

class DavyJonesControlPanel extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this._config = {};
    this._rules = {};
    this._dirty = false;
  }

  getViewType() { return CONTROL_VIEW_TYPE; }
  getDisplayText() { return "DavyJones Control Panel"; }
  getIcon() { return "sliders-horizontal"; }

  async onOpen() { this._render(); }
  async onClose() { this.contentEl.empty(); }

  _render() {
    const el = this.contentEl;
    el.empty();
    el.addClass("davyjones-cp-root");
    this._config = this.plugin._readDavyJonesEnv();
    this._rules = this.plugin._readVaultRules();
    this._dirty = false;

    // Scroll wrapper (header + body scroll together, apply bar stays pinned)
    const scroll = el.createDiv({ cls: "davyjones-cp-scroll" });

    // ── Header ──
    const header = scroll.createDiv({ cls: "davyjones-cp-header" });
    header.createEl("h2", { text: "DavyJones Control Panel" });
    header.createEl("p", { cls: "davyjones-cp-subtitle", text: "Configure agent behavior, MCP services, and vault rules." });

    const body = scroll.createDiv({ cls: "davyjones-cp-body" });

    // ── MCP Services ──
    const mcpSection = body.createDiv({ cls: "davyjones-cp-section" });
    mcpSection.createEl("h3", { text: "MCP Services" });
    mcpSection.createEl("p", { cls: "davyjones-cp-desc", text: "Click a service to configure tokens and additional accounts." });

    const mcpGrid = mcpSection.createDiv({ cls: "davyjones-cp-mcp-grid" });

    for (const service of CP_SERVICE_DEFS) {
      const enabledKey = `${service.id.toUpperCase()}_MCP_ENABLED`;
      const hasToken = !!this._config[service.tokenKey];
      const isEnabled = this._config[enabledKey] !== "false";
      const instanceCount = (this._rules.serviceInstances || []).filter(i => i.service === service.id).length;

      const card = mcpGrid.createDiv({ cls: "davyjones-cp-mcp-card davyjones-cp-mcp-card-clickable" });
      const cardTop = card.createDiv({ cls: "davyjones-cp-mcp-card-top" });
      const dot = cardTop.createEl("span", { cls: "davyjones-dot" });
      dot.addClass(hasToken && isEnabled ? "davyjones-dot-on" : "davyjones-dot-off");
      cardTop.createEl("span", { cls: "davyjones-cp-mcp-name", text: service.name });

      const statusText = !hasToken ? "no token" : isEnabled ? "active" : "paused";
      cardTop.createEl("span", {
        cls: `davyjones-cp-mcp-status ${!hasToken ? "is-notoken" : isEnabled ? "is-active" : "is-paused"}`,
        text: statusText,
      });

      if (instanceCount > 0) {
        cardTop.createEl("span", { cls: "davyjones-cp-mcp-badge", text: `+${instanceCount}` });
      }

      const cardBody = card.createDiv({ cls: "davyjones-cp-mcp-card-body" });
      cardBody.createEl("p", { cls: "davyjones-cp-mcp-desc", text: service.desc });

      if (hasToken) {
        const toggleRow = cardBody.createDiv({ cls: "davyjones-cp-mcp-toggle-row" });
        new Setting(toggleRow)
          .setName("Active")
          .addToggle((toggle) => {
            toggle.setValue(isEnabled).onChange((value) => {
              this._config[enabledKey] = value ? "true" : "false";
              this._dirty = true;
              dot.removeClass("davyjones-dot-on", "davyjones-dot-off");
              dot.addClass(value ? "davyjones-dot-on" : "davyjones-dot-off");
              const st = card.querySelector(".davyjones-cp-mcp-status");
              if (st) {
                st.setText(value ? "active" : "paused");
                st.className = `davyjones-cp-mcp-status ${value ? "is-active" : "is-paused"}`;
              }
            });
          });
        // Stop toggle clicks from opening modal
        toggleRow.addEventListener("click", (e) => e.stopPropagation());
      } else {
        cardBody.createEl("p", { cls: "davyjones-cp-mcp-hint", text: `Click to add a ${service.name} token.` });
      }

      // Click card → open config modal
      card.addEventListener("click", () => {
        new DavyJonesMCPConfigModal(
          this.app, this.plugin, service.id,
          this._config, this._rules,
          (updatedConfig, updatedInstances) => {
            // Merge config updates
            Object.assign(this._config, updatedConfig);
            // Replace instances for this service, keep others
            const otherInstances = (this._rules.serviceInstances || []).filter(i => i.service !== service.id);
            this._rules.serviceInstances = [...otherInstances, ...updatedInstances.filter(i => i.id && i.token)];
            this._dirty = true;
            this._render();
          },
        ).open();
      });
    }

    // ── Agent Behavior ──
    const agentSection = body.createDiv({ cls: "davyjones-cp-section" });
    agentSection.createEl("h3", { text: "Agent Behavior" });

    new Setting(agentSection)
      .setName("Custom Instructions")
      .setDesc("Injected into every overseer and agent prompt (e.g., language, coding style, constraints).")
      .addTextArea((text) => {
        text.setPlaceholder('e.g., "Always respond in Romanian", "Prefer Python over JS"')
          .setValue(this._rules.customInstructions || "")
          .onChange((value) => { this._rules.customInstructions = value; this._dirty = true; });
        text.inputEl.rows = 5;
        text.inputEl.style.width = "100%";
      });

    new Setting(agentSection)
      .setName("Response Verbosity")
      .setDesc("Controls how detailed agent output is.")
      .addDropdown((drop) => {
        drop.addOption("concise", "Concise")
          .addOption("normal", "Normal")
          .addOption("detailed", "Detailed")
          .setValue(this._rules.verbosity || "normal")
          .onChange((value) => { this._rules.verbosity = value; this._dirty = true; });
      });

    const tuningRow = agentSection.createDiv({ cls: "davyjones-cp-row" });

    const turnsBox = tuningRow.createDiv({ cls: "davyjones-cp-inline-setting" });
    new Setting(turnsBox)
      .setName("Max Turns")
      .setDesc("Per-agent turn budget")
      .addText((text) => {
        text.setPlaceholder("20")
          .setValue(String(this._rules.maxTurns || 20))
          .onChange((value) => { this._rules.maxTurns = parseInt(value, 10) || 20; this._dirty = true; });
        text.inputEl.type = "number";
        text.inputEl.style.width = "70px";
      });

    const timeoutBox = tuningRow.createDiv({ cls: "davyjones-cp-inline-setting" });
    new Setting(timeoutBox)
      .setName("Timeout (sec)")
      .setDesc("Max time per agent")
      .addText((text) => {
        text.setPlaceholder("300")
          .setValue(String(this._rules.timeout || 300))
          .onChange((value) => { this._rules.timeout = parseInt(value, 10) || 300; this._dirty = true; });
        text.inputEl.type = "number";
        text.inputEl.style.width = "70px";
      });

    new Setting(agentSection)
      .setName("Auto-commit")
      .setDesc("Automatically commit vault changes made by agents.")
      .addToggle((toggle) => {
        toggle.setValue(this._rules.autoCommit === true)
          .onChange((value) => { this._rules.autoCommit = value; this._dirty = true; });
      });

    // ── Ignore Patterns ──
    const ignoreSection = body.createDiv({ cls: "davyjones-cp-section" });
    ignoreSection.createEl("h3", { text: "Ignore Patterns" });

    new Setting(ignoreSection)
      .setName("Files to Skip")
      .setDesc("Glob patterns the overseer ignores, one per line.")
      .addTextArea((text) => {
        text.setPlaceholder("_templates/*\ndaily/*\n.obsidian/*")
          .setValue((this._rules.ignorePatterns || []).join("\n"))
          .onChange((value) => {
            this._rules.ignorePatterns = value.split("\n").map(s => s.trim()).filter(Boolean);
            this._dirty = true;
          });
        text.inputEl.rows = 4;
        text.inputEl.style.width = "100%";
      });

    // ── Allowed Operations ──
    const opsSection = body.createDiv({ cls: "davyjones-cp-section" });
    opsSection.createEl("h3", { text: "Allowed Operations" });
    opsSection.createEl("p", { cls: "davyjones-cp-desc", text: "Restrict what agents can do in this vault." });

    const ops = this._rules.allowedOperations || {};
    const opsGrid = opsSection.createDiv({ cls: "davyjones-cp-ops-grid" });
    for (const [key, label, icon] of [
      ["createFiles", "Create Files", "file-plus"],
      ["deleteFiles", "Delete Files", "file-minus"],
      ["modifyFiles", "Modify Files", "file-edit"],
      ["runGitCommands", "Git Commands", "git-branch"],
    ]) {
      const opCard = opsGrid.createDiv({ cls: `davyjones-cp-op-card ${ops[key] !== false ? "is-allowed" : "is-denied"}` });
      opCard.createEl("span", { cls: "davyjones-cp-op-label", text: label });
      new Setting(opCard).addToggle((toggle) => {
        toggle.setValue(ops[key] !== false).onChange((value) => {
          if (!this._rules.allowedOperations) this._rules.allowedOperations = {};
          this._rules.allowedOperations[key] = value;
          this._dirty = true;
          opCard.classList.toggle("is-allowed", value);
          opCard.classList.toggle("is-denied", !value);
        });
      });
    }

    // ── Custom Secrets ──
    const secretsSection = body.createDiv({ cls: "davyjones-cp-section" });
    secretsSection.createEl("h3", { text: "Custom Secrets" });
    secretsSection.createEl("p", { cls: "davyjones-cp-desc", text: "Environment variables injected into every agent container (e.g., GCP keys, Strapi passwords)." });

    if (!this._rules.secrets) this._rules.secrets = {};
    const secretsContainer = secretsSection.createDiv({ cls: "davyjones-cp-secrets-list" });

    const renderSecrets = () => {
      secretsContainer.empty();
      const entries = Object.entries(this._rules.secrets);

      for (const [key, value] of entries) {
        const row = secretsContainer.createDiv({ cls: "davyjones-cp-secrets-row" });

        const keyInput = row.createEl("input", {
          type: "text",
          cls: "davyjones-cp-secrets-key",
          placeholder: "KEY_NAME",
          value: key,
        });

        const valueInput = row.createEl("input", {
          type: "password",
          cls: "davyjones-cp-secrets-value",
          placeholder: "secret value",
        });
        valueInput.value = value;

        const revealBtn = row.createEl("button", { cls: "davyjones-cp-secrets-reveal", text: "Show" });
        revealBtn.addEventListener("click", () => {
          const isPassword = valueInput.type === "password";
          valueInput.type = isPassword ? "text" : "password";
          revealBtn.setText(isPassword ? "Hide" : "Show");
        });

        const delBtn = row.createEl("button", { cls: "davyjones-cp-secrets-del", text: "Delete" });
        delBtn.addEventListener("click", () => {
          delete this._rules.secrets[key];
          this._dirty = true;
          renderSecrets();
        });

        // On key change: re-key the entry
        keyInput.addEventListener("change", () => {
          const newKey = keyInput.value.trim().toUpperCase().replace(/[^A-Z0-9_]/g, "_");
          if (newKey && newKey !== key) {
            delete this._rules.secrets[key];
            this._rules.secrets[newKey] = valueInput.value;
            this._dirty = true;
            renderSecrets();
          }
        });

        valueInput.addEventListener("input", () => {
          this._rules.secrets[key] = valueInput.value;
          this._dirty = true;
        });
      }

      if (entries.length === 0) {
        secretsContainer.createEl("p", { cls: "davyjones-cp-secrets-empty", text: "No secrets configured. Click \"+\" to add one." });
      }
    };

    renderSecrets();

    const addSecretBtn = secretsSection.createEl("button", { cls: "davyjones-cp-add-btn", text: "+ Add Secret" });
    addSecretBtn.addEventListener("click", () => {
      // Generate a unique placeholder key
      let idx = 1;
      while (this._rules.secrets[`SECRET_${idx}`]) idx++;
      this._rules.secrets[`SECRET_${idx}`] = "";
      this._dirty = true;
      renderSecrets();
    });

    // ── Apply bar ──
    const applyBar = el.createDiv({ cls: "davyjones-cp-apply-bar" });
    const applyBtn = applyBar.createEl("button", { cls: "davyjones-cp-apply-btn", text: "Apply & Restart" });
    applyBtn.addEventListener("click", () => {
      this.plugin._writeDavyJonesEnv(this._config);
      this.plugin._writeVaultRules(this._rules);
      this.plugin._applyServiceConfig();
      this._dirty = false;
      new Notice("Configuration saved. Restarting services...");
    });
  }
}

const CP_SERVICE_DEFS = [
  { id: "github", name: "GitHub", tokenKey: "GITHUB_TOKEN", desc: "Repos, issues, PRs, actions, code search.", hint: "GitHub > Settings > Developer settings > Personal access tokens", prefix: "ghp_", supportsMulti: true },
  { id: "gitlab", name: "GitLab", tokenKey: "GITLAB_TOKEN", desc: "Repos, issues, MRs, files, branches.", hint: "GitLab > Settings > Access Tokens", prefix: "glpat-", supportsMulti: true },
  { id: "slack", name: "Slack", tokenKey: "SLACK_BOT_TOKEN", desc: "Channels, messages, reactions, users, search.", hint: "api.slack.com/apps > OAuth & Permissions", prefix: "xoxb-", supportsMulti: false },
];

// ─── MCP Configuration Modal ──────────────────────────────────

class DavyJonesMCPConfigModal extends Modal {
  constructor(app, plugin, serviceId, config, rules, onSave) {
    super(app);
    this.plugin = plugin;
    this.serviceId = serviceId;
    this.serviceDef = CP_SERVICE_DEFS.find(s => s.id === serviceId);
    this._config = { ...config };
    this._rules = rules;
    this._onSave = onSave;
    // Deep-clone the instances for this service so cancel doesn't persist
    this._instances = (rules.serviceInstances || [])
      .filter(i => i.service === serviceId)
      .map(i => ({ ...i, config: { ...(i.config || {}) } }));
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass("davyjones-mcp-modal");
    this._renderContent();
  }

  onClose() {
    this.contentEl.empty();
  }

  _renderContent() {
    const el = this.contentEl;
    el.empty();

    el.createEl("h2", { text: `Configure ${this.serviceDef.name}` });

    // ── Primary Token ──
    const primarySection = el.createDiv({ cls: "davyjones-mcp-modal-primary" });
    primarySection.createEl("h4", { text: "Primary Token" });
    if (this.serviceDef.hint) {
      primarySection.createEl("p", { cls: "davyjones-mcp-modal-hint", text: this.serviceDef.hint });
    }

    const primaryRow = primarySection.createDiv({ cls: "davyjones-mcp-modal-token-row" });
    const primaryInput = primaryRow.createEl("input", {
      type: "password",
      cls: "davyjones-mcp-modal-input",
      placeholder: this.serviceDef.prefix ? `${this.serviceDef.prefix}...` : "Token",
    });
    primaryInput.value = this._config[this.serviceDef.tokenKey] || "";
    primaryInput.addEventListener("input", () => {
      this._config[this.serviceDef.tokenKey] = primaryInput.value;
    });

    const revealBtn = primaryRow.createEl("button", { cls: "davyjones-mcp-modal-reveal", text: "Show" });
    revealBtn.addEventListener("click", () => {
      const isPassword = primaryInput.type === "password";
      primaryInput.type = isPassword ? "text" : "password";
      revealBtn.setText(isPassword ? "Hide" : "Show");
    });

    // ── Additional Accounts (only for multi-capable services) ──
    if (this.serviceDef.supportsMulti) {
      const instancesSection = el.createDiv({ cls: "davyjones-mcp-modal-instances" });
      instancesSection.createEl("h4", { text: "Additional Accounts" });
      instancesSection.createEl("p", { cls: "davyjones-mcp-modal-hint", text: "Agents connect to all accounts simultaneously." });

      const instancesList = instancesSection.createDiv({ cls: "davyjones-mcp-modal-instances-list" });

      for (let i = 0; i < this._instances.length; i++) {
        this._renderInstance(instancesList, i);
      }

      const addBtn = instancesSection.createEl("button", { cls: "davyjones-cp-add-btn", text: "+ Add Account" });
      addBtn.addEventListener("click", () => {
        this._instances.push({
          id: "",
          service: this.serviceId,
          label: "",
          token: "",
          config: {},
        });
        this._renderContent();
      });
    }

    // ── Actions ──
    const actions = el.createDiv({ cls: "davyjones-mcp-modal-actions" });

    const saveBtn = actions.createEl("button", { cls: "davyjones-mcp-modal-save", text: "Save" });
    saveBtn.addEventListener("click", () => {
      this._save();
      this.close();
    });

    const cancelBtn = actions.createEl("button", { cls: "davyjones-mcp-modal-cancel", text: "Cancel" });
    cancelBtn.addEventListener("click", () => {
      this.close();
    });
  }

  _renderInstance(container, index) {
    const inst = this._instances[index];
    const card = container.createDiv({ cls: "davyjones-mcp-modal-instance" });

    const headerRow = card.createDiv({ cls: "davyjones-mcp-modal-instance-header" });
    headerRow.createEl("span", { cls: "davyjones-mcp-modal-instance-num", text: `Account ${index + 1}` });
    const delBtn = headerRow.createEl("button", { cls: "davyjones-mcp-modal-instance-del", text: "Delete" });
    delBtn.addEventListener("click", () => {
      this._instances.splice(index, 1);
      this._renderContent();
    });

    const fields = card.createDiv({ cls: "davyjones-mcp-modal-instance-fields" });

    // Label
    new Setting(fields)
      .setName("Label")
      .setDesc("Friendly name (e.g., \"Work\", \"Personal\")")
      .addText((text) => {
        text.setPlaceholder("e.g., Work GitHub")
          .setValue(inst.label || "")
          .onChange((value) => {
            inst.label = value;
            inst.id = value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
          });
      });

    // Token
    new Setting(fields)
      .setName("Token")
      .addText((text) => {
        text.setPlaceholder(this.serviceDef.prefix ? `${this.serviceDef.prefix}...` : "Token")
          .setValue(inst.token || "")
          .onChange((value) => { inst.token = value; });
        text.inputEl.type = "password";
      });

    // GitLab-specific: API URL
    if (this.serviceId === "gitlab") {
      new Setting(fields)
        .setName("API URL")
        .setDesc("For self-hosted GitLab instances")
        .addText((text) => {
          text.setPlaceholder("https://gitlab.com")
            .setValue((inst.config && inst.config.apiUrl) || "")
            .onChange((value) => {
              if (!inst.config) inst.config = {};
              inst.config.apiUrl = value;
            });
        });
    }
  }

  _save() {
    // Update the primary token in the env config
    this._onSave(this._config, this._instances);
  }
}

// ─── Settings Tab ─────────────────────────────────────────────

const SERVICE_DEFS = [
  {
    id: "github",
    name: "GitHub",
    keys: [
      {
        key: "GITHUB_TOKEN",
        label: "Token",
        desc: "Repos, issues, PRs, actions, code search.",
        hint: "GitHub > Settings > Developer settings > Personal access tokens",
        prefix: "ghp_",
      },
      {
        key: "GITHUB_REPO",
        label: "Monitor Repo",
        desc: "Auto-update vault when this repo changes (polls every 60s).",
        hint: "Format: owner/repo (e.g., octocat/my-project)",
        prefix: "",
        optional: true,
      },
    ],
  },
  {
    id: "gitlab",
    name: "GitLab",
    keys: [
      {
        key: "GITLAB_TOKEN",
        label: "Token",
        desc: "Repos, issues, MRs, files, branches.",
        hint: "GitLab > Settings > Access Tokens",
        prefix: "glpat-",
      },
      {
        key: "GITLAB_MCP_URL",
        label: "API URL",
        desc: "Only change for self-hosted GitLab (default: gitlab.com).",
        hint: "",
        prefix: "",
        optional: true,
      },
    ],
  },
  {
    id: "slack",
    name: "Slack",
    keys: [
      {
        key: "SLACK_BOT_TOKEN",
        label: "Bot Token",
        desc: "Channels, messages, reactions, users, search, pins.",
        hint: "api.slack.com/apps > OAuth & Permissions",
        prefix: "xoxb-",
      },
      {
        key: "SLACK_APP_TOKEN",
        label: "App Token",
        desc: "Enables interactive @mention bot (Socket Mode).",
        hint: "api.slack.com/apps > Socket Mode",
        prefix: "xapp-",
        optional: true,
      },
    ],
  },
];

class DavyJonesSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
    this._dirty = false;
    this._config = {};
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    this._config = this.plugin._readDavyJonesEnv();
    this._dirty = false;

    containerEl.createEl("h2", { text: "DavyJones Settings" });

    // ── Claude Auth section ──
    const authHeader = containerEl.createDiv({ cls: "davyjones-service-header" });
    const authDot = authHeader.createEl("span", { cls: "davyjones-dot" });
    const authValue = this._config["CLAUDE_CODE_OAUTH_TOKEN"] || "";
    authDot.addClass(authValue ? "davyjones-dot-on" : "davyjones-dot-off");
    authHeader.createEl("span", { text: "Claude Auth", cls: "davyjones-service-name" });
    authHeader.createEl("span", {
      text: authValue ? "long-lived token" : "using OAuth (expires)",
      cls: `davyjones-service-status ${authValue ? "is-enabled" : "is-disabled"}`,
    });

    const authSetting = new Setting(containerEl)
      .setName("Auth Token")
      .setDesc("Long-lived token from 'claude setup-token' (valid 1 year). Without this, OAuth is used and may expire.");

    authSetting.addText((text) => {
      text
        .setPlaceholder("sk-ant-oat01-...")
        .setValue(authValue)
        .onChange((value) => {
          this._config["CLAUDE_CODE_OAUTH_TOKEN"] = value.trim();
          this._dirty = true;
          const hasVal = !!value.trim();
          authDot.removeClass("davyjones-dot-on", "davyjones-dot-off");
          authDot.addClass(hasVal ? "davyjones-dot-on" : "davyjones-dot-off");
          const statusEl = authHeader.querySelector(".davyjones-service-status");
          if (statusEl) {
            statusEl.setText(hasVal ? "long-lived token" : "using OAuth (expires)");
            statusEl.classList.toggle("is-enabled", hasVal);
            statusEl.classList.toggle("is-disabled", !hasVal);
          }
        });
      if (authValue) text.inputEl.type = "password";
      text.inputEl.addEventListener("focus", () => { text.inputEl.type = "text"; });
      text.inputEl.addEventListener("blur", () => {
        if (text.inputEl.value) text.inputEl.type = "password";
      });
    });
    if (authValue) {
      authSetting.addButton((btn) => {
        btn.setIcon("x").setTooltip("Clear token").onClick(() => {
          this._config["CLAUDE_CODE_OAUTH_TOKEN"] = "";
          this._dirty = true;
          this.display();
        });
      });
    }

    // ── MCP Services section ──
    const desc = containerEl.createEl("p", {
      cls: "setting-item-description davyjones-settings-desc",
    });
    desc.setText("Configure which MCP services are available to agents. A token enables the service — clear it to disable.");

    for (const service of SERVICE_DEFS) {
      // Service header
      const header = containerEl.createDiv({ cls: "davyjones-service-header" });
      const dot = header.createEl("span", { cls: "davyjones-dot" });
      const hasToken = service.keys.some(
        (k) => !k.optional && this._config[k.key]
      );
      dot.addClass(hasToken ? "davyjones-dot-on" : "davyjones-dot-off");
      header.createEl("span", {
        text: service.name,
        cls: "davyjones-service-name",
      });
      header.createEl("span", {
        text: hasToken ? "enabled" : "disabled",
        cls: `davyjones-service-status ${hasToken ? "is-enabled" : "is-disabled"}`,
      });

      // Token fields
      for (const keyDef of service.keys) {
        const currentValue = this._config[keyDef.key] || "";
        const setting = new Setting(containerEl)
          .setName(keyDef.label)
          .setDesc(keyDef.desc + (keyDef.hint ? ` (${keyDef.hint})` : ""));

        setting.addText((text) => {
          text
            .setPlaceholder(keyDef.prefix ? `${keyDef.prefix}...` : "")
            .setValue(currentValue)
            .onChange((value) => {
              this._config[keyDef.key] = value.trim();
              this._dirty = true;
              // Update the status dot
              const newHasToken = service.keys.some(
                (k) => !k.optional && this._config[k.key]
              );
              dot.removeClass("davyjones-dot-on", "davyjones-dot-off");
              dot.addClass(newHasToken ? "davyjones-dot-on" : "davyjones-dot-off");
              const statusEl = header.querySelector(".davyjones-service-status");
              if (statusEl) {
                statusEl.setText(newHasToken ? "enabled" : "disabled");
                statusEl.classList.toggle("is-enabled", newHasToken);
                statusEl.classList.toggle("is-disabled", !newHasToken);
              }
            });

          // Mask token values
          if (keyDef.prefix && currentValue) {
            text.inputEl.type = "password";
          }
          text.inputEl.addEventListener("focus", () => {
            text.inputEl.type = "text";
          });
          text.inputEl.addEventListener("blur", () => {
            if (text.inputEl.value && keyDef.prefix) {
              text.inputEl.type = "password";
            }
          });
        });

        // Add clear button if value exists
        if (currentValue && !keyDef.optional) {
          setting.addButton((btn) => {
            btn
              .setIcon("x")
              .setTooltip("Clear token")
              .onClick(() => {
                this._config[keyDef.key] = "";
                this._dirty = true;
                this.display(); // Re-render
              });
          });
        }
      }

    }

    // ── Apply button ──
    const applyContainer = containerEl.createDiv({ cls: "davyjones-apply-container" });
    new Setting(applyContainer)
      .setName("")
      .setDesc("Saves tokens and restarts services. Use the Control Panel for agent behavior settings.")
      .addButton((btn) => {
        btn
          .setButtonText("Save & Restart")
          .setCta()
          .onClick(() => {
            this.plugin._writeDavyJonesEnv(this._config);
            this.plugin._applyServiceConfig();
            this._dirty = false;
          });
      });

    new Setting(applyContainer)
      .setName("")
      .setDesc("")
      .addButton((btn) => {
        btn
          .setButtonText("Open Control Panel")
          .onClick(() => this.plugin._activateControlPanel());
      });
  }
}

module.exports = DavyJonesPlugin;
