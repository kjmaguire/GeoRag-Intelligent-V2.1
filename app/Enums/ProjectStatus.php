<?php

declare(strict_types=1);

namespace App\Enums;

enum ProjectStatus: string
{
    case Active = 'active';
    case Indexing = 'indexing';
    case Degraded = 'degraded';
    case Archived = 'archived';
}
