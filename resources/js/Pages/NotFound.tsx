import type { JSX } from 'react';
import { Head, Link } from '@inertiajs/react';

/**
 * 404 Not Found page — styled to match the dark theme.
 */

interface NotFoundProps {}

export default function NotFound(_props: NotFoundProps): JSX.Element {
    return (
        <>
            <Head title="404 — Not Found" />
            <div className="min-h-screen bg-gray-950 flex items-center justify-center px-4">
                <div className="text-center">
                    <div className="text-6xl font-bold text-gray-700 mb-4">404</div>
                    <h1 className="text-xl font-semibold text-gray-200 mb-2">Page not found</h1>
                    <p className="text-sm text-gray-500 mb-6">
                        The page you're looking for doesn't exist or has been moved.
                    </p>
                    <div className="flex items-center justify-center gap-3">
                        <Link
                            href="/chat"
                            className="bg-amber-600 hover:bg-amber-500 text-white font-medium rounded-lg px-5 py-2.5 text-sm transition-colors"
                        >
                            Go to Chat
                        </Link>
                        <Link
                            href="/"
                            className="border border-gray-700 hover:border-gray-500 text-gray-300 font-medium rounded-lg px-5 py-2.5 text-sm transition-colors"
                        >
                            Home
                        </Link>
                    </div>
                </div>
            </div>
        </>
    );
}
