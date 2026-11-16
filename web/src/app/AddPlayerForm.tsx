"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { addPlayer, ApiError, describeError, normalizeUsername, usingFixtures } from "@/lib/api";
import { Button } from "@/components/Button";
import styles from "./landing.module.css";

export function AddPlayerForm() {
  const router = useRouter();
  const search = useSearchParams();
  const [value, setValue] = useState(search.get("u") ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const username = normalizeUsername(value);
    if (!username) {
      setError(
        value.trim() === ""
          ? "Enter your Chess.com username."
          : "Chess.com usernames are letters, numbers, dashes and underscores.",
      );
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const p = await addPlayer(username);
      const games = p.time_classes.reduce((n, c) => n + c.games, 0);
      // A brand-new player has nothing to report yet: show the download instead.
      router.push(games > 0 ? `/players/${username}` : `/players/${username}/progress`);
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 501) {
        setError(`Adding players from the website isn't switched on yet. Run "chess-coach-sync ${username}" instead.`);
      } else {
        setError(describeError(err));
      }
    }
  }

  return (
    <form className={styles.form} onSubmit={onSubmit} noValidate>
      <label htmlFor="username" className="visually-hidden">
        Chess.com username
      </label>
      <div className={styles.field}>
        <input
          id="username"
          name="username"
          className={styles.input}
          placeholder="Chess.com username"
          autoComplete="username"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          maxLength={60}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-invalid={error !== null}
          aria-describedby={error ? "username-error" : usingFixtures ? "username-hint" : undefined}
          disabled={busy}
        />
        <Button type="submit" disabled={busy} className={styles.submit}>
          {busy ? "Adding…" : "Analyze my games"}
        </Button>
      </div>
      {error ? (
        <p id="username-error" className={styles.error} role="alert">
          {error}
        </p>
      ) : (
        usingFixtures && (
          <p id="username-hint" className={styles.hint}>
            Sample data: try <strong>deitythemenace</strong>, or <strong>newplayer</strong> for a fresh account.
          </p>
        )
      )}
    </form>
  );
}
