import { describe, expect, it } from 'vitest';
import {
    DEFAULT_UPLOAD_LIMIT,
    describeUploadFailure,
    exceedsUploadLimit,
    uploadLimitFromProps,
} from '@/lib/uploadLimit';

const LIMIT = { bytes: 512 * 1024 * 1024, human: '512 MB' };

describe('upload limit (FE-2)', () => {
    it('reads the server-shared prop, falling back to the server default', () => {
        expect(uploadLimitFromProps({ upload_limit: { bytes: 1024, human: '1 KB' } })).toEqual({
            bytes: 1024,
            human: '1 KB',
        });
        expect(uploadLimitFromProps({})).toEqual(DEFAULT_UPLOAD_LIMIT);
        expect(uploadLimitFromProps({ upload_limit: { bytes: 0 } })).toEqual(DEFAULT_UPLOAD_LIMIT);
    });

    it('a 900 MB file is over a 512 MB limit', () => {
        expect(exceedsUploadLimit(900 * 1024 * 1024, LIMIT)).toBe(true);
        expect(exceedsUploadLimit(LIMIT.bytes, LIMIT)).toBe(false);
    });

    it('maps a 413 or a dropped connection on a big file to the limit', () => {
        expect(describeUploadFailure(413, undefined, 10, LIMIT)).toBe('Exceeds the 512 MB upload limit.');
        expect(describeUploadFailure(null, undefined, LIMIT.bytes, LIMIT)).toBe('Exceeds the 512 MB upload limit.');
        expect(describeUploadFailure(null, undefined, 1024, LIMIT)).toMatch(/network error/i);
    });

    it('prefers the server message otherwise', () => {
        expect(describeUploadFailure(422, 'The file must be a shapefile bundle.', 10, LIMIT)).toBe(
            'The file must be a shapefile bundle.',
        );
        expect(describeUploadFailure(500, undefined, 10, LIMIT)).toBe('Upload failed (HTTP 500).');
    });
});
