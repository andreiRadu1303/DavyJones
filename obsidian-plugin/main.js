const { Plugin, Notice, MarkdownView, PluginSettingTab, Setting } = require("obsidian");
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
    }, 15000));
  }

  onunload() {
    document.querySelectorAll(".davyjones-nav").forEach((el) => el.remove());
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
      { header: "# GitHub", keys: ["GITHUB_TOKEN", "GITHUB_REPO"] },
      { header: "# GitLab", keys: ["GITLAB_TOKEN", "GITLAB_MCP_URL"] },
      { header: "# Slack", keys: ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"] },
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

  _applyServiceConfig() {
    if (!this._projectRoot) {
      new Notice("DavyJones project path not configured.");
      return;
    }

    new Notice("Applying service configuration...");

    const shell = process.env.SHELL || "/bin/bash";
    const mergeScript = path.join(this._projectRoot, "scripts", "_merge_env.sh");
    const rootEnv = path.join(this._projectRoot, ".env");
    const cmd = `${shell} -l -c 'source "${mergeScript}" && merge_vault_env "${this._vaultPath}" "${rootEnv}" && cd "${this._projectRoot}" && docker compose restart dispatcher 2>&1'`;

    exec(cmd, { timeout: 60000, cwd: this._projectRoot }, (err, stdout, stderr) => {
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

    const cmd = 'git add -A && git commit -m "DavyJones: vault update"';
    exec(cmd, { cwd: this._vaultPath, timeout: 15000 }, (err, stdout, stderr) => {
      this._committing = false;
      if (err) {
        console.error("DavyJones commit error:", stderr || err.message);
        new Notice("Commit failed: " + (stderr?.split("\n")[0] || err.message));
      } else {
        new Notice("Changes committed");
      }
      this._updateGitStatus();
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
    const cmd = `${shell} -l -c 'bash "${fixScript}" && cd "${this._projectRoot}" && docker compose restart dispatcher'`;

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
      .setDesc("Saves to .davyjones.env and restarts the dispatcher.")
      .addButton((btn) => {
        btn
          .setButtonText("Apply Changes")
          .setCta()
          .onClick(() => {
            this.plugin._writeDavyJonesEnv(this._config);
            this.plugin._applyServiceConfig();
            this._dirty = false;
          });
      });
  }
}

module.exports = DavyJonesPlugin;
