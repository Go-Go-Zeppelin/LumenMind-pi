-- Schema 1 is executed by memory.repository.MemoryRepository.initialize().
-- This marker is versioned alongside the repository so deployments can audit the
-- authority schema without relying on an untracked SQLite file.
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
