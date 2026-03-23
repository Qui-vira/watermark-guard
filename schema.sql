-- WatermarkGuard Supabase Schema (prefixed with wg_ to avoid conflicts)

-- Groups table
CREATE TABLE IF NOT EXISTS wg_groups (
    id BIGINT PRIMARY KEY,  -- Telegram chat_id
    title TEXT NOT NULL,
    watermark_type TEXT CHECK (watermark_type IN ('text', 'logo', 'both')),
    watermark_text TEXT,
    watermark_url TEXT,
    watermark_position TEXT DEFAULT 'bottom-right' CHECK (watermark_position IN ('center', 'bottom-right', 'bottom-left', 'top-right', 'top-left', 'banner')),
    watermark_rotation INTEGER DEFAULT 0,
    use_channel_name BOOLEAN DEFAULT FALSE,
    logo_path TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Pending images table
CREATE TABLE IF NOT EXISTS wg_pending_images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id BIGINT REFERENCES wg_groups(id) ON DELETE CASCADE,
    admin_id BIGINT NOT NULL,
    original_file_id TEXT NOT NULL,
    watermarked_file_id TEXT,
    watermarked_path TEXT,
    original_caption TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_wg_pending_images_status ON wg_pending_images(status);
CREATE INDEX IF NOT EXISTS idx_wg_pending_images_group_id ON wg_pending_images(group_id);
CREATE INDEX IF NOT EXISTS idx_wg_pending_images_admin_id ON wg_pending_images(admin_id);

-- RLS
ALTER TABLE wg_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_pending_images ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on wg_groups"
    ON wg_groups FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Service role full access on wg_pending_images"
    ON wg_pending_images FOR ALL
    USING (true)
    WITH CHECK (true);
