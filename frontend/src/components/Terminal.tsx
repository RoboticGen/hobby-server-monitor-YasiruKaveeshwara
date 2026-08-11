/**
 * Minimal terminal for one container.
 *
 * A command input, a Run button, and an append-only transcript of what came
 * back. Each submission is one POST to /api/containers/{id}/exec, which runs
 * the command and returns its stdout, stderr and exit code as JSON.
 *
 * WHY THERE IS NO xterm.js (OR ANY TERMINAL EMULATOR):
 * A terminal emulator exists to render a PTY byte stream — cursor movement,
 * colour escapes, line editing — and that only means anything if there is a
 * persistent bidirectional connection to a live shell on the other end. This
 * backend deliberately offers no such thing. Its exec endpoint takes one argv
 * array, runs it, and answers; that request/response shape is exactly what
 * makes shell injection impossible (decision 7.7), so it is a feature, not a
 * limitation to paper over.
 *
 * Bolting an emulator onto a request/response endpoint would draw a shell that
 * is not there: no prompt state, no working directory carried between
 * commands, no interactive programs (vim, top, less), no Ctrl-C. Someone would
 * type `cd /var`, see it succeed, and then be baffled when the next command
 * ran somewhere else. So this UI is honest about what the endpoint is — a
 * command runner with a transcript — which is also exactly the brief's
 * baseline: send a command, see the result.
 */
import { useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import { apiFetch, ApiError } from "../lib/api";

/** Response body of POST /api/containers/{id}/exec on success. */
interface ExecResponse {
	container_id: string;
	exit_code: number;
	stdout: string;
	stderr: string;
}

/**
 * What happened for one submitted command.
 *
 * A discriminated union rather than one shape with nullable fields, so a call
 * that never ran cannot be misread as a command that exited 0 with no output.
 * Those are different events and the type keeps them apart.
 */
type LogOutcome =
	| {
			kind: "result";
			argv: string[];
			exitCode: number;
			stdout: string;
			stderr: string;
	  }
	| { kind: "error"; argv: string[]; message: string };

/** An outcome once it is in the log, with a stable key for React. */
type LogEntry = LogOutcome & { id: number };

export interface TerminalProps {
	/** Container UUID — the {container_id} in the exec path. */
	containerId: string;
}

/**
 * Drop trailing newlines from a captured stream.
 *
 * Command output almost always ends with one, and the transcript adds its own
 * break between sections — without this, every command would leave a stray
 * blank line behind it.
 */
function trimTrailingNewlines(text: string): string {
	return text.replace(/\n+$/, "");
}

export default function Terminal({ containerId }: TerminalProps) {
	const [commandText, setCommandText] = useState<string>("");
	const [log, setLog] = useState<LogEntry[]>([]);
	const [running, setRunning] = useState<boolean>(false);

	// Keys come from a counter rather than the array index so an entry keeps
	// its identity no matter what happens to the list around it.
	const nextId = useRef<number>(0);

	// The scrolling transcript, so a new entry can bring itself into view.
	const logRef = useRef<HTMLPreElement>(null);

	/**
	 * Keep the newest output visible.
	 *
	 * The log only ever grows and the page caps its height, so without this the
	 * result of every command after the first would land below the fold and the
	 * terminal would look like it had stopped answering.
	 */
	useEffect(() => {
		const element = logRef.current;
		if (element) element.scrollTop = element.scrollHeight;
	}, [log]);

	/** Append one outcome to the transcript, giving it the next key. */
	function appendEntry(outcome: LogOutcome): void {
		const entry: LogEntry = { ...outcome, id: nextId.current++ };
		setLog((previous) => [...previous, entry]);
	}

	/**
	 * Run whatever is in the input.
	 *
	 * `preventDefault` first: the default form action is a full page navigation,
	 * which would tear down this island and the page around it.
	 */
	async function handleSubmit(event: SyntheticEvent<HTMLFormElement>) {
		event.preventDefault();

		// The input stays enabled while a command runs (see the render below),
		// so Enter can be pressed again mid-flight. This is the guard that keeps
		// that from firing a second request.
		if (running) return;

		// -------------------------------------------------------------------
		// SPLIT INTO AN ARRAY — this is the one line to get right.
		//
		// The endpoint accepts ONLY {"command": ["echo", "hello"]} and answers
		// 400 for a plain string. That rejection is the security boundary, not
		// tidiness: a string is only useful to a shell, and a shell is what
		// turns `ls; rm -rf /` into two commands. As an argv array handed
		// straight to pylxd's exec there is no shell to inject into, so the
		// second half is just another argument. The mistake to avoid is
		// sending `{ command: commandText }` — it type-checks fine here and
		// fails at the server.
		//
		// The split is whitespace only, so quoting is NOT supported: typing
		// echo "a b" sends ["echo", "\"a", "b\""] and the container's echo
		// prints the quote marks as characters. That is deliberate — parsing
		// quotes here would mean writing a shell-argument parser in the
		// browser, which is precisely the thing decision 7.7 removed. An
		// argument containing a space cannot be expressed in this UI.
		// -------------------------------------------------------------------
		const trimmed = commandText.trim();

		// Guard before splitting, because "".split(/\s+/) is [""] — an array of
		// one empty string, which passes the backend's array check and its
		// non-empty check, then tries to run a program with no name.
		if (!trimmed) return;

		const argv = trimmed.split(/\s+/);

		setRunning(true);

		// Cleared immediately, the way a shell clears its line on Enter. Nothing
		// is lost: the transcript echoes the argv that was sent.
		setCommandText("");

		try {
			const result = await apiFetch<ExecResponse>(
				`/api/containers/${encodeURIComponent(containerId)}/exec`,
				{
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ command: argv }),
				},
			);

			appendEntry({
				kind: "result",
				argv,
				exitCode: result.exit_code,
				stdout: result.stdout,
				stderr: result.stderr,
			});
		} catch (err) {
			// Failures go into the transcript rather than a separate banner, so
			// the record stays in order: you can see which command was refused
			// and what came before it. The server's own wording is passed
			// through, which is what surfaces "You do not have access to this
			// container." for a container the caller was never assigned —
			// rather than a silent no-op that looks like the command simply
			// produced no output.
			appendEntry({
				kind: "error",
				argv,
				message:
					err instanceof ApiError
						? `Request failed (${err.status}): ${err.message}`
						: "Could not reach the server to run the command.",
			});
		} finally {
			// In `finally` so a refused command re-enables Run instead of
			// leaving the terminal stuck on "Running…".
			setRunning(false);
		}
	}

	/**
	 * Render one transcript entry as terminal text.
	 *
	 * The newlines are explicit text nodes: inside <pre> the line breaks that
	 * count are the ones in the content, and JSX discards the ones that sit
	 * between elements in the source.
	 */
	function renderEntry(entry: LogEntry): ReactNode {
		// The echoed command, so the log reads as a session even though every
		// command was an independent request. argv is joined for display only —
		// it is a record of the array that was sent, never re-parsed.
		const prompt = <span className='prompt'>{`$ ${entry.argv.join(" ")}\n`}</span>;

		if (entry.kind === "error") {
			return (
				<span key={entry.id}>
					{prompt}
					<span className='failure'>{`${entry.message}\n`}</span>
					{"\n"}
				</span>
			);
		}

		const stdout = trimTrailingNewlines(entry.stdout);
		const stderr = trimTrailingNewlines(entry.stderr);

		return (
			<span key={entry.id}>
				{prompt}
				{stdout && `${stdout}\n`}
				{stderr && <span className='stderr'>{`${stderr}\n`}</span>}
				{/* Shown even on success: a command that prints nothing and exits
				    0 would otherwise leave no evidence it ran at all. */}
				<span className={entry.exitCode === 0 ? "exit" : "exit failure"}>
					{`exit ${entry.exitCode}\n`}
				</span>
				{"\n"}
			</span>
		);
	}

	return (
		<div className='terminal'>
			<form className='terminal-input' onSubmit={handleSubmit}>
				<label htmlFor={`terminal-command-${containerId}`}>Command</label>
				<input
					id={`terminal-command-${containerId}`}
					type='text'
					value={commandText}
					placeholder='ls -la /etc'
					autoComplete='off'
					spellCheck={false}
					// Deliberately NOT disabled while running: disabling a focused
					// input hands focus back to the document, so after every
					// command the cursor would have to be clicked into place again.
					// handleSubmit guards the in-flight case instead.
					onChange={(event) => setCommandText(event.target.value)}
				/>
				<button type='submit' disabled={running || commandText.trim() === ""}>
					{running ? "Running…" : "Run"}
				</button>
			</form>

			{/* Stated up front rather than discovered by a confused user, since
			    the whitespace split is not what a shell would do. */}
			<p className='hint'>
				One command per run, split on spaces and sent as an array. Quoting is not
				supported, and nothing carries over between commands — there is no shell
				session here.
			</p>

			{/* role="log" is the live-region role for exactly this: an
			    append-only transcript. It tells assistive tech to announce new
			    output without re-reading the whole history. tabIndex makes the
			    scrolling region reachable by keyboard. */}
			<pre className='terminal-log' ref={logRef} role='log' aria-label='Command output' tabIndex={0}>
				{log.length === 0 ? "No commands run yet." : log.map(renderEntry)}
			</pre>
		</div>
	);
}
