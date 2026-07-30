import { useState, type FormEvent, type JSX } from 'react';
import { Head, Link } from '@inertiajs/react';

interface ResetPasswordProps {
    token: string;
    email: string;
}

interface ResetPasswordApiResponse {
    message?: string;
    errors?: Record<string, string[]>;
}

export default function ResetPassword({ token, email }: ResetPasswordProps): JSX.Element {
    const [password, setPassword] = useState('');
    const [confirmation, setConfirmation] = useState('');
    const [completed, setCompleted] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);

    async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
        event.preventDefault();
        setLoading(true);
        setError(null);

        try {
            const response = await fetch('/api/v1/auth/reset-password', {
                method: 'POST',
                headers: {
                    Accept: 'application/json',
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    token,
                    email,
                    password,
                    password_confirmation: confirmation,
                }),
            });
            const data: ResetPasswordApiResponse = await response.json();
            if (!response.ok) {
                const validationError = Object.values(data.errors ?? {})[0]?.[0];
                throw new Error(validationError ?? data.message ?? 'Password reset failed');
            }
            setCompleted(true);
        } catch (caught) {
            setError(caught instanceof Error ? caught.message : 'Password reset failed');
        } finally {
            setLoading(false);
        }
    }

    return (
        <>
            <Head title="Choose a new password" />
            <div className="flex min-h-screen items-center justify-center bg-gray-950 px-4">
                <div className="w-full max-w-sm">
                    <div className="mb-6 text-center">
                        <h1 className="text-xl font-semibold text-gray-100">
                            Choose a new password
                        </h1>
                        <p className="mt-1 text-sm text-gray-500">{email}</p>
                    </div>

                    {completed ? (
                        <div className="rounded-xl border border-green-800/40 bg-green-950/40 p-6 text-center">
                            <p className="mb-3 text-sm text-green-300">
                                Your password has been reset.
                            </p>
                            <Link
                                href="/login"
                                className="text-xs text-amber-400 underline hover:text-amber-300"
                            >
                                Continue to login
                            </Link>
                        </div>
                    ) : (
                        <form
                            onSubmit={handleSubmit}
                            className="flex flex-col gap-4 rounded-xl border border-gray-800 bg-gray-900 p-6 shadow-xl"
                        >
                            {error && (
                                <div className="rounded-lg border border-red-800/50 bg-red-950/50 px-3 py-2 text-sm text-red-400">
                                    {error}
                                </div>
                            )}

                            <label className="flex flex-col gap-1.5 text-xs font-medium text-gray-400">
                                New password
                                <input
                                    type="password"
                                    value={password}
                                    onChange={(event) => setPassword(event.target.value)}
                                    required
                                    autoComplete="new-password"
                                    className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2.5 text-sm text-gray-100 focus:ring-2 focus:ring-amber-500 focus:outline-none"
                                />
                            </label>

                            <label className="flex flex-col gap-1.5 text-xs font-medium text-gray-400">
                                Confirm password
                                <input
                                    type="password"
                                    value={confirmation}
                                    onChange={(event) => setConfirmation(event.target.value)}
                                    required
                                    autoComplete="new-password"
                                    className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2.5 text-sm text-gray-100 focus:ring-2 focus:ring-amber-500 focus:outline-none"
                                />
                            </label>

                            <button
                                type="submit"
                                disabled={loading}
                                className="rounded-lg bg-amber-600 py-2.5 text-sm font-medium text-white transition-colors hover:bg-amber-500 disabled:bg-gray-700"
                            >
                                {loading ? 'Resetting…' : 'Reset password'}
                            </button>
                        </form>
                    )}
                </div>
            </div>
        </>
    );
}
