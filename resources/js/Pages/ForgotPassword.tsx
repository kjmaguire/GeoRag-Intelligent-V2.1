import { useState, type JSX } from 'react';
import { Head, Link } from '@inertiajs/react';

/**
 * Forgot Password page — sends a reset link to the user's email.
 * Uses Laravel's built-in password reset functionality via Sanctum.
 */

interface ForgotPasswordProps {}

interface ForgotPasswordApiResponse {
    message?: string;
}

export default function ForgotPassword(_props: ForgotPasswordProps): JSX.Element {
    const [email, setEmail] = useState<string>('');
    const [sent, setSent] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState<boolean>(false);

    async function handleSubmit(e: React.FormEvent<HTMLFormElement>): Promise<void> {
        e.preventDefault();
        setLoading(true);
        setError(null);

        try {
            const res = await fetch('/api/v1/auth/forgot-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ email }),
            });
            const data: ForgotPasswordApiResponse = await res.json();
            if (!res.ok) throw new Error(data.message ?? 'Request failed');
            setSent(true);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Request failed');
        } finally {
            setLoading(false);
        }
    }

    return (
        <>
            <Head title="Forgot Password" />
            <div className="min-h-screen bg-gray-950 flex items-center justify-center px-4">
                <div className="w-full max-w-sm">
                    <div className="text-center mb-6">
                        <h1 className="text-xl font-semibold text-gray-100">Reset Password</h1>
                        <p className="text-sm text-gray-500 mt-1">
                            Enter your email and we'll send you a reset link.
                        </p>
                    </div>

                    {sent ? (
                        <div className="bg-green-950/40 border border-green-800/40 rounded-xl p-6 text-center">
                            <p className="text-sm text-green-300 mb-3">
                                If an account exists for <strong>{email}</strong>, a password reset link has been sent.
                            </p>
                            <Link href="/login" className="text-xs text-amber-400 hover:text-amber-300 underline">
                                Back to login
                            </Link>
                        </div>
                    ) : (
                        <form onSubmit={handleSubmit} className="bg-gray-900 border border-gray-800 rounded-xl p-6 shadow-xl space-y-4">
                            {error && (
                                <div className="text-sm text-red-400 bg-red-950/50 border border-red-800/50 rounded-lg px-3 py-2">
                                    {error}
                                </div>
                            )}
                            <div>
                                <label htmlFor="email" className="block text-xs text-gray-400 mb-1.5 font-medium">Email</label>
                                <input
                                    id="email"
                                    type="email"
                                    value={email}
                                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEmail(e.target.value)}
                                    required
                                    autoFocus
                                    className="w-full bg-gray-800 text-gray-100 border border-gray-700 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500"
                                    placeholder="you@company.com"
                                />
                            </div>
                            <button
                                type="submit"
                                disabled={loading}
                                className="w-full bg-amber-600 hover:bg-amber-500 disabled:bg-gray-700 text-white font-medium rounded-lg py-2.5 text-sm transition-colors"
                            >
                                {loading ? 'Sending…' : 'Send Reset Link'}
                            </button>
                            <p className="text-xs text-gray-600 text-center">
                                <Link href="/login" className="text-gray-400 hover:text-gray-200 underline">Back to login</Link>
                            </p>
                        </form>
                    )}
                </div>
            </div>
        </>
    );
}
