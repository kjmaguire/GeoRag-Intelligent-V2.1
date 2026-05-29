<?php

declare(strict_types=1);

namespace App\Http\Resources;

use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\JsonResource;

class ProjectResource extends JsonResource
{
    public function toArray(Request $request): array
    {
        return [
            'project_id' => $this->project_id,
            'project_name' => $this->project_name,
            'slug' => $this->slug,
            'crs_datum' => $this->crs_datum,
            'company' => $this->company,
            'commodity' => $this->commodity,
            'region' => $this->region,
            'magnetic_declination' => $this->magnetic_declination,
            'orientation_reference' => $this->orientation_reference,
            // collar_count is appended via withCount('collars') in the controller,
            // which Eloquent exposes as the attribute `collars_count`.
            'collar_count' => $this->collars_count ?? 0,
            'created_at' => $this->created_at?->toISOString(),
            'updated_at' => $this->updated_at?->toISOString(),
        ];
    }
}
