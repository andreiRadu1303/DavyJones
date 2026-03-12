const { Plugin, Notice, MarkdownView, PluginSettingTab, Setting, ItemView } = require("obsidian");
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
const HISTORY_PAGE_SIZE = 20;

class DavyJonesPlugin extends Plugin {
  async onload() {
    this._vaultPath = this.app.vault.adapter.basePath;
    this._projectRoot = this._loadProjectRoot();
    this._gitDirty = false;
    this._gitChangeCount = 0;
    this._committing = false;

    // Send to Claude
    this.addRibbonIcon("send", "DavyJones: Send to Claude", () => this.sendToClaude());
    this.addCommand({ id: "send-to-claude", name: "Send to Claude", callback: () => this.sendToClaude() });

    // Commit command
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
    }, 15000));
  }

  onunload() {
    document.querySelectorAll(".davyjones-nav").forEach((el) => el.remove());
    this.app.workspace.detachLeavesOfType(HISTORY_VIEW_TYPE);
    this.app.workspace.detachLeavesOfType(CONTROL_VIEW_TYPE);
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

    if (this._committing) {
      this._gitBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-commit" });
      this._gitBarEl.createEl("span", { text: "committing...", cls: "davyjones-statusbar-text" });
      this._gitBarEl.style.cursor = "default";
      return;
    }

    if (this._gitDirty) {
      this._gitBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-dirty" });
      this._gitBarEl.createEl("span", {
        text: `${this._gitChangeCount} change${this._gitChangeCount !== 1 ? "s" : ""}`,
        cls: "davyjones-statusbar-text",
      });
      this._gitBarEl.style.cursor = "pointer";
    } else {
      this._gitBarEl.createEl("span", { cls: "davyjones-dot davyjones-dot-clean" });
      this._gitBarEl.createEl("span", { text: "committed", cls: "davyjones-statusbar-text" });
      this._gitBarEl.style.cursor = "default";
    }
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

    const cmd = 'git add -A && git -c user.name="DavyJones" -c user.email="davyjones@local" commit -m "DavyJones: vault update"';
    exec(cmd, { cwd: this._vaultPath, timeout: 15000 }, (err, stdout, stderr) => {
      this._committing = false;
      if (err) {
        console.error("DavyJones commit error:", stderr || err.message);
        new Notice("Commit failed: " + (stderr?.split("\n")[0] || err.message));
      } else {
        new Notice("Changes committed");
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

  // ─── Send to Claude ────────────────────────────────────────

  async sendToClaude() {
    const file = this.app.workspace.getActiveFile();
    if (!file) return new Notice("No active file");

    if (!this._isVaultActive()) {
      new Notice("Vault is not active. Click 'DavyJones (offline)' in the status bar to switch.");
      return;
    }

    await this.app.fileManager.processFrontMatter(file, (fm) => {
      if (fm.status === "pending") return new Notice("Already pending");
      if (fm.status === "in_progress") return new Notice("Task is running");
      fm.type = "task";
      fm.status = "pending";
      delete fm.completed_at;
      delete fm.error_message;
    });
    new Notice("Sent to Claude — commit to dispatch");
    // Refresh git status since frontmatter changed
    setTimeout(() => this._updateGitStatus(), 500);
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
      const statusBadge = props.createEl("span", {
        cls: `davyjones-prop-badge davyjones-s-${fm.status}`,
        text: fm.status.replace("_", " "),
      });
      if (fm.status === "completed" || fm.status === "failed") {
        statusBadge.classList.add("is-clickable");
        statusBadge.addEventListener("click", () => this.sendToClaude());
      }
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
    mcpSection.createEl("p", { cls: "davyjones-cp-desc", text: "Toggle which integrations agents can use. Tokens are configured in Settings > DavyJones." });

    const mcpGrid = mcpSection.createDiv({ cls: "davyjones-cp-mcp-grid" });

    for (const service of CP_SERVICE_DEFS) {
      const enabledKey = `${service.id.toUpperCase()}_MCP_ENABLED`;
      const hasToken = !!this._config[service.tokenKey];
      const isEnabled = this._config[enabledKey] !== "false";

      const card = mcpGrid.createDiv({ cls: "davyjones-cp-mcp-card" });
      const cardTop = card.createDiv({ cls: "davyjones-cp-mcp-card-top" });
      const dot = cardTop.createEl("span", { cls: "davyjones-dot" });
      dot.addClass(hasToken && isEnabled ? "davyjones-dot-on" : "davyjones-dot-off");
      cardTop.createEl("span", { cls: "davyjones-cp-mcp-name", text: service.name });

      const statusText = !hasToken ? "no token" : isEnabled ? "active" : "paused";
      cardTop.createEl("span", {
        cls: `davyjones-cp-mcp-status ${!hasToken ? "is-notoken" : isEnabled ? "is-active" : "is-paused"}`,
        text: statusText,
      });

      const cardBody = card.createDiv({ cls: "davyjones-cp-mcp-card-body" });
      cardBody.createEl("p", { cls: "davyjones-cp-mcp-desc", text: service.desc });

      if (hasToken) {
        new Setting(cardBody)
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
      } else {
        cardBody.createEl("p", { cls: "davyjones-cp-mcp-hint", text: `Add a ${service.name} token in Settings > DavyJones to enable.` });
      }
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
  { id: "github", name: "GitHub", tokenKey: "GITHUB_TOKEN", desc: "Repos, issues, PRs, actions, code search." },
  { id: "gitlab", name: "GitLab", tokenKey: "GITLAB_TOKEN", desc: "Repos, issues, MRs, files, branches." },
  { id: "slack", name: "Slack", tokenKey: "SLACK_BOT_TOKEN", desc: "Channels, messages, reactions, users, search." },
];

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
