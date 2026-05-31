-- Add missing columns to blogs table for admin panel support
ALTER TABLE blogs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'published' CHECK (status IN ('draft', 'published'));
ALTER TABLE blogs ADD COLUMN IF NOT EXISTS cover_image TEXT;
ALTER TABLE blogs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- Create storage bucket for blog cover images (if using Supabase Storage)
INSERT INTO storage.buckets (id, name, public) VALUES ('blog-covers', 'blog-covers', true) ON CONFLICT DO NOTHING;

-- Allow public read access to blog cover images
CREATE POLICY "Public read blog covers" ON storage.objects
    FOR SELECT USING (bucket_id = 'blog-covers');
