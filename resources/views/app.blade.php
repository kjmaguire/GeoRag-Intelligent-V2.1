<!DOCTYPE html>
<html lang="{{ str_replace('_', '-', app()->getLocale()) }}">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="csrf-token" content="{{ csrf_token() }}">

        <title inertia>{{ config('app.name', 'GeoRAG Intelligence') }}</title>

        <!-- Fonts -->
        <link rel="preconnect" href="https://fonts.bunny.net">
        <link href="https://fonts.bunny.net/css?family=figtree:400,500,600&display=swap" rel="stylesheet" />

        {{-- Map tile server preconnect — eliminates DNS + TLS handshake latency
             on first tile request. These connections are established in parallel
             with page load so they're warm by the time MapView renders.

             Derived from config('services.basemap') rather than listed: these
             were three literals naming hosts that a repointed deployment does
             not use, and two of them named hosts the CSP did not allow, so the
             page warmed a connection the browser would then refuse to fetch
             over. One source of truth for what the maps talk to. --}}
        @foreach (\App\Support\BasemapAssets::origins() as $host)
            <link rel="preconnect" href="{{ $host }}" crossorigin>
            <link rel="dns-prefetch" href="{{ $host }}">
        @endforeach

        <!-- Scripts -->
        @viteReactRefresh
        @vite(['resources/css/app.css', 'resources/js/app.tsx'])
        @inertiaHead
    </head>
    <body class="font-sans antialiased foundry">
        @inertia
    </body>
</html>
