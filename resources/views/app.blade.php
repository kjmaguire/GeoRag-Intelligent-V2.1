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

        <!-- Map tile server preconnect — eliminates DNS + TLS handshake latency
             on first tile request. These connections are established in parallel
             with page load so they're warm by the time MapView renders. -->
        <link rel="preconnect" href="https://tiles.openfreemap.org" crossorigin>
        <link rel="preconnect" href="https://tiles.maps.eox.at" crossorigin>
        <link rel="preconnect" href="https://tiles.mapterhorn.com" crossorigin>
        <link rel="dns-prefetch" href="https://tiles.openfreemap.org">
        <link rel="dns-prefetch" href="https://tiles.maps.eox.at">
        <link rel="dns-prefetch" href="https://tiles.mapterhorn.com">

        <!-- Scripts -->
        @viteReactRefresh
        @vite(['resources/css/app.css', 'resources/js/app.tsx'])
        @inertiaHead
    </head>
    <body class="font-sans antialiased foundry">
        @inertia
    </body>
</html>
