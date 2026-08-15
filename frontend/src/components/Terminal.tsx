/**
 * Interactive Web Terminal for executing commands inside a container.
 *
 * Designed to look, feel, and behave like a real Linux terminal:
 * - Direct inline shell prompt ($ / #) with blinking cursor
 * - Command history with Up / Down arrow key navigation
 * - Built-in shell utilities (clear, history, help, reset)
 * - POSIX quote parsing into an argv array (no shell — see parseCommand)
 * - Tab autocompletion for common commands
 * - Keyboard shortcuts (Ctrl+L to clear, Ctrl+C to cancel)
 * - Click-to-focus terminal surface
 */
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

export interface TerminalProps {
	containerId: string;
	lxdName?: string;
	initialLog?: LogEntry[];
}

export type LogEntry =
	| {
			id: number;
			kind: "result";
			argv: string[];
			rawCommand: string;
			exitCode: number;
			stdout: string;
			stderr: string;
	  }
	| {
			id: number;
			kind: "error";
			argv: string[];
			rawCommand: string;
			message: string;
	  }
	| {
			id: number;
			kind: "system";
			message: string;
	  };

export type LogEntryInput =
	| {
			kind: "result";
			argv: string[];
			rawCommand: string;
			exitCode: number;
			stdout: string;
			stderr: string;
	  }
	| {
			kind: "error";
			argv: string[];
			rawCommand: string;
			message: string;
	  }
	| {
			kind: "system";
			message: string;
	  };

interface ExecResponse {
	exit_code: number;
	stdout: string;
	stderr: string;
}

interface QuickCommand {
	label: string;
	command: string;
	description: string;
}

const QUICK_COMMANDS: QuickCommand[] = [
	{ label: "OS Info", command: "cat /etc/os-release", description: "Distribution & kernel info" },
	{ label: "Memory", command: "free -h", description: "RAM & swap usage" },
	{ label: "Disk", command: "df -h /", description: "Root filesystem headroom" },
	{ label: "Processes", command: "ps aux", description: "Active container processes" },
	{ label: "Network", command: "ip -br addr", description: "Assigned network interfaces" },
	{ label: "Uptime", command: "uptime", description: "Container uptime & load averages" },
];

const AUTOCOMPLETE_COMMANDS = [
	"cat",
	"ls",
	"ps",
	"top",
	"df",
	"free",
	"ip",
	"uname",
	"uptime",
	"clear",
	"history",
	"help",
	"grep",
	"head",
	"tail",
	"mkdir",
	"rm",
	"touch",
	"chmod",
	"chown",
	"curl",
	"wget",
	"ping",
	"netstat",
	"ss",
	"systemctl",
	"journalctl",
	"env",
	"export",
	"whoami",
	"pwd",
	"id",
	"find",
	"wc",
	"tar",
	"gzip",
];

/**
 * Characters that carry meaning only to a shell interpreter.
 *
 * There is no shell on the execution path: the backend hands the argv array
 * straight to LXD. So an *unquoted* operator here cannot be honoured, and the
 * only two options are to refuse it or to smuggle in a shell to interpret it.
 * Refusing is the entire injection defense, so refusing is what happens.
 */
const SHELL_OPERATORS = ["|", "&", ">", "<", ";"];

/**
 * Outcome of parsing one input line into an argv array.
 *
 * A discriminated union rather than a thrown error or a `string[] | null`,
 * because the caller has to render the *reason* back into the terminal log —
 * a bare failure would leave the user staring at a line that did nothing.
 */
export type ParseResult = { ok: true; argv: string[] } | { ok: false; reason: string };

/**
 * Split a command line into an argv array, refusing shell syntax.
 *
 * Quoting is respected, and that distinction is the whole point: a *quoted*
 * operator is ordinary text and is passed through as one literal argv element
 * (`echo "a; b"` is a legitimate command), while an *unquoted* one is shell
 * syntax that this transport cannot express and is refused. That mirrors the
 * backend exactly — see the pair of tests in test_security.py that require
 * string commands to be rejected but metacharacters inside a single element
 * to survive unsplit.
 */
function parseCommand(input: string): ParseResult {
	const trimmed = input.trim();
	if (!trimmed) return { ok: false, reason: "No command to run." };

	const tokens: string[] = [];
	let current = "";
	let inSingle = false;
	let inDouble = false;
	let escaped = false;

	for (let i = 0; i < trimmed.length; i++) {
		const char = trimmed[i];

		if (escaped) {
			current += char;
			escaped = false;
			continue;
		}

		if (char === "\\") {
			escaped = true;
			continue;
		}

		if (char === "'" && !inDouble) {
			inSingle = !inSingle;
			continue;
		}

		if (char === '"' && !inSingle) {
			inDouble = !inDouble;
			continue;
		}

		// Checked before the whitespace split so the operator is caught while
		// the quote state that makes it syntax-or-data is still known.
		if (!inSingle && !inDouble && SHELL_OPERATORS.includes(char)) {
			return {
				ok: false,
				reason:
					`Shell operator '${char}' is not supported. Commands run as a direct argv array ` +
					`with no shell, so pipes, redirects and chains cannot be interpreted. ` +
					`Quote it to send it as literal text, or run one command at a time.`,
			};
		}

		if (/\s/.test(char) && !inSingle && !inDouble) {
			if (current.length > 0) {
				tokens.push(current);
				current = "";
			}
			continue;
		}

		current += char;
	}

	// Previously an unclosed quote silently dropped the quote character and ran
	// anyway, so `echo "hi` became ["echo", "hi"]. Saying so beats guessing.
	if (inSingle || inDouble) {
		return { ok: false, reason: "Unterminated quote in command." };
	}

	if (current.length > 0) {
		tokens.push(current);
	}

	// Reachable for input that is entirely empty quotes, e.g. `''`.
	if (tokens.length === 0) {
		return { ok: false, reason: "No command to run." };
	}

	return { ok: true, argv: tokens };
}

function trimTrailingNewlines(text: string): string {
	return text.replace(/\n+$/, "");
}

export default function Terminal({ containerId, lxdName, initialLog = [] }: TerminalProps) {
	const instanceName = lxdName || containerId.slice(0, 8);
	const promptUser = `root@${instanceName}`;

	const [commandText, setCommandText] = useState<string>("");
	const [log, setLog] = useState<LogEntry[]>(initialLog);
	const [running, setRunning] = useState<boolean>(false);
	const [copiedNotice, setCopiedNotice] = useState<boolean>(false);
	const [isFullscreen, setIsFullscreen] = useState<boolean>(false);

	// Command history navigation
	const [history, setHistory] = useState<string[]>([]);
	const [historyIndex, setHistoryIndex] = useState<number>(-1);

	const terminalRef = useRef<HTMLDivElement | null>(null);
	const inputRef = useRef<HTMLInputElement | null>(null);
	const logRef = useRef<HTMLDivElement | null>(null);
	const nextId = useRef<number>(initialLog.length + 1);

	// Scroll to bottom on log updates
	useEffect(() => {
		const el = logRef.current;
		if (el) {
			el.scrollTop = el.scrollHeight;
		}
	}, [log, running]);

	// Initial welcome message
	useEffect(() => {
		if (initialLog.length === 0 && log.length === 0) {
			setLog([
				{
					id: 0,
					kind: "system",
					message: `Connected to unprivileged container '${instanceName}' (${containerId.slice(0, 8)}).\nType 'help' for terminal features or 'clear' to clear screen.`,
				},
			]);
		}
	}, [containerId, instanceName]);

	function appendEntry(entry: LogEntryInput): void {
		const withId = { ...entry, id: nextId.current } as LogEntry;
		nextId.current += 1;
		setLog((prev) => [...prev, withId]);
	}

	function handleClear(): void {
		setLog([]);
		setCommandText("");
		focusInput();
	}

	function handleReset(): void {
		setLog([
			{
				id: nextId.current++,
				kind: "system",
				message: `Terminal session reset for '${instanceName}'.\nType 'help' for available shortcuts and commands.`,
			},
		]);
		setCommandText("");
		focusInput();
	}

	function focusInput(): void {
		inputRef.current?.focus();
	}

	async function copySessionLog(): Promise<void> {
		const text = log
			.map((entry) => {
				if (entry.kind === "system") return `[system] ${entry.message}`;
				if (entry.kind === "error") return `$ ${entry.rawCommand}\n[error] ${entry.message}`;
				const out = entry.stdout ? `${entry.stdout}\n` : "";
				const err = entry.stderr ? `[stderr] ${entry.stderr}\n` : "";
				return `$ ${entry.rawCommand}\n${out}${err}[exit ${entry.exitCode}]`;
			})
			.join("\n\n");

		try {
			await navigator.clipboard.writeText(text);
			setCopiedNotice(true);
			setTimeout(() => setCopiedNotice(false), 2000);
		} catch {
			// ignore clipboard error
		}
	}

	async function execute(cmdString: string): Promise<void> {
		const trimmed = cmdString.trim();
		if (!trimmed || running) return;

		// Add to history buffer
		setHistory((prev) => [...prev, trimmed]);
		setHistoryIndex(-1);
		setCommandText("");

		// Built-in Shell commands
		const lower = trimmed.toLowerCase();
		if (lower === "clear" || lower === "cls") {
			handleClear();
			return;
		}

		if (lower === "history") {
			const historyList = [...history, trimmed]
				.map((cmd, i) => `  ${String(i + 1).padStart(3, " ")}  ${cmd}`)
				.join("\n");
			appendEntry({
				kind: "result",
				argv: ["history"],
				rawCommand: trimmed,
				exitCode: 0,
				stdout: historyList || "No history yet.",
				stderr: "",
			});
			return;
		}

		if (lower === "help") {
			const helpText = [
				"Container Terminal Commands & Shortcuts:",
				"  clear, cls       Clear the terminal output screen (or Ctrl+L)",
				"  history          Display list of executed commands",
				"  help             Display this help guide",
				"  Up / Down Arrow  Cycle through command history",
				"  Tab              Autocomplete common Linux commands",
				"  Ctrl + C         Clear current input line",
				"",
				"Quick Examples:",
				"  uname -a, free -h, df -h /, ps aux, ip addr, cat /etc/os-release",
				"",
				"Note: commands run as a direct argv array with no shell, so the",
				"operators  |  &  >  <  ;  are refused. Quote them to send literal",
				'text (echo "a; b"), or run one command at a time.',
			].join("\n");

			appendEntry({
				kind: "result",
				argv: ["help"],
				rawCommand: trimmed,
				exitCode: 0,
				stdout: helpText,
				stderr: "",
			});
			return;
		}

		const parsed = parseCommand(trimmed);
		if (!parsed.ok) {
			// Refused before any network call — the command never reaches the
			// container, and the user sees why rather than a dead prompt.
			appendEntry({
				kind: "error",
				argv: [],
				rawCommand: trimmed,
				message: parsed.reason,
			});
			return;
		}

		const argv = parsed.argv;
		setRunning(true);

		try {
			const result = await apiFetch<ExecResponse>(`/api/containers/${encodeURIComponent(containerId)}/exec`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ command: argv }),
			});

			appendEntry({
				kind: "result",
				argv,
				rawCommand: trimmed,
				exitCode: result.exit_code,
				stdout: result.stdout,
				stderr: result.stderr,
			});
		} catch (err) {
			appendEntry({
				kind: "error",
				argv,
				rawCommand: trimmed,
				message:
					err instanceof ApiError ?
						`(${err.status}): ${err.message}`
					:	"Could not reach the server to execute command.",
			});
		} finally {
			setRunning(false);
			setTimeout(focusInput, 50);
		}
	}

	function handleFormSubmit(e: SyntheticEvent<HTMLFormElement>): void {
		e.preventDefault();
		void execute(commandText);
	}

	function handleKeyDown(e: KeyboardEvent<HTMLInputElement>): void {
		// Ctrl + L -> Clear screen
		if (e.ctrlKey && e.key.toLowerCase() === "l") {
			e.preventDefault();
			handleClear();
			return;
		}

		// Ctrl + C -> Clear current line
		if (e.ctrlKey && e.key.toLowerCase() === "c") {
			e.preventDefault();
			setCommandText("");
			return;
		}

		// Up Arrow -> Previous command
		if (e.key === "ArrowUp") {
			e.preventDefault();
			if (history.length === 0) return;
			const nextIdx = historyIndex === -1 ? history.length - 1 : Math.max(0, historyIndex - 1);
			setHistoryIndex(nextIdx);
			setCommandText(history[nextIdx] || "");
			return;
		}

		// Down Arrow -> Next command
		if (e.key === "ArrowDown") {
			e.preventDefault();
			if (historyIndex === -1) return;
			if (historyIndex >= history.length - 1) {
				setHistoryIndex(-1);
				setCommandText("");
			} else {
				const nextIdx = historyIndex + 1;
				setHistoryIndex(nextIdx);
				setCommandText(history[nextIdx] || "");
			}
			return;
		}

		// Tab Autocomplete
		if (e.key === "Tab") {
			e.preventDefault();
			const trimmed = commandText.trim();
			if (!trimmed) return;
			const match = AUTOCOMPLETE_COMMANDS.find((cmd) => cmd.startsWith(trimmed));
			if (match) {
				setCommandText(match + " ");
			}
		}
	}

	function renderEntry(entry: LogEntry): ReactNode {
		if (entry.kind === "system") {
			return (
				<div key={entry.id} className='term-entry term-system-entry'>
					<span className='system-msg'>{entry.message}</span>
				</div>
			);
		}

		const promptLine = (
			<div className='term-prompt-line'>
				<span className='term-user'>{promptUser}</span>
				<span className='term-colon'>:</span>
				<span className='term-path'>~</span>
				<span className='term-char'>#</span>
				<span className='term-cmd-text'>{entry.rawCommand}</span>
			</div>
		);

		if (entry.kind === "error") {
			return (
				<div key={entry.id} className='term-entry term-error-block'>
					{promptLine}
					<div className='term-err-output'>
						<span className='err-symbol'>✖</span> {entry.message}
					</div>
				</div>
			);
		}

		const stdout = trimTrailingNewlines(entry.stdout);
		const stderr = trimTrailingNewlines(entry.stderr);

		return (
			<div key={entry.id} className='term-entry'>
				{promptLine}
				{stdout && <div className='term-stdout'>{stdout}</div>}
				{stderr && <div className='term-stderr'>{stderr}</div>}
				{entry.exitCode !== 0 && (
					<div className='term-exit-tag failed'>[process exited with code {entry.exitCode}]</div>
				)}
			</div>
		);
	}

	return (
		<div ref={terminalRef} className={`real-terminal-shell ${isFullscreen ? "fullscreen" : ""}`} onClick={focusInput}>
			{/* Terminal Window Header Bar */}
			<header className='terminal-titlebar' onClick={(e) => e.stopPropagation()}>
				<div className='titlebar-controls'>
					<button type='button' className='window-btn close' onClick={handleClear} title='Clear screen (Ctrl+L)' />
					<button type='button' className='window-btn minimize' onClick={handleReset} title='Reset session' />
					<button
						type='button'
						className='window-btn expand'
						onClick={() => setIsFullscreen(!isFullscreen)}
						title='Toggle Fullscreen'
					/>
				</div>

				<div className='titlebar-center mono'>
					<span className='shell-icon'>⚡</span>
					<span className='shell-title'>{promptUser}: ~ (no shell)</span>
				</div>

				<div className='titlebar-actions'>
					{copiedNotice && <span className='copy-toast'>Copied!</span>}
					<button type='button' className='titlebar-tool-btn' onClick={copySessionLog} title='Copy full session output'>
						Copy
					</button>
					<button type='button' className='titlebar-tool-btn' onClick={handleClear} title='Clear terminal'>
						Clear
					</button>
					<span className={`status-pill ${running ? "executing" : "ready"}`}>{running ? "Running…" : "Ready"}</span>
				</div>
			</header>

			{/* Quick Shortcuts Pill Toolbar */}
			<div className='terminal-shortcuts-bar' onClick={(e) => e.stopPropagation()}>
				<span className='shortcuts-label'>Quick shortcuts:</span>
				<div className='shortcuts-list'>
					{QUICK_COMMANDS.map((qc) => (
						<button
							key={qc.command}
							type='button'
							className='shortcut-chip'
							onClick={() => void execute(qc.command)}
							title={qc.description}>
							{qc.label}
						</button>
					))}
				</div>
			</div>

			{/* Scrollable Terminal Output Body */}
			<div className='terminal-viewport' ref={logRef}>
				{log.map(renderEntry)}

				{/* Active Interactive Shell Prompt Line */}
				<form className='interactive-prompt-form' onSubmit={handleFormSubmit}>
					<div className='active-prompt-label mono'>
						<span className='term-user'>{promptUser}</span>
						<span className='term-colon'>:</span>
						<span className='term-path'>~</span>
						<span className='term-char'>#</span>
					</div>

					<div className='prompt-input-wrapper'>
						<input
							ref={inputRef}
							type='text'
							className='terminal-live-input mono'
							value={commandText}
							onChange={(e) => setCommandText(e.target.value)}
							onKeyDown={handleKeyDown}
							autoComplete='off'
							autoCorrect='off'
							autoCapitalize='off'
							spellCheck={false}
							disabled={running}
							placeholder={running ? "Executing command…" : ""}
						/>
						{!running && <span className='blinking-cursor' />}
					</div>

					<button
						type='submit'
						className='terminal-run-key'
						disabled={running || commandText.trim() === ""}
						title='Execute (Enter)'>
						↵
					</button>
				</form>
			</div>

			<footer className='terminal-status-footer'>
				<span>
					Type <kbd>help</kbd> for options • <kbd>↑</kbd> / <kbd>↓</kbd> history • <kbd>Tab</kbd> autocomplete •{" "}
					<kbd>Ctrl+L</kbd> clear
				</span>
			</footer>
		</div>
	);
}
