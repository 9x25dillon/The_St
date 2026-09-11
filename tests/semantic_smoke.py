"""Optional full pipeline check after setup and model download; synthetic data only."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from app import import_data, semantic_map
from mirror import cluster, read_demo, render
import tempfile

records, vectors = read_demo()
labels, scores, positions = cluster(vectors)
assert positions.shape == (len(records), 2)
assert len(set(labels) - {-1}) >= 4, 'Planted themes failed to separate'
assert np.isfinite(positions).all()
with tempfile.TemporaryDirectory() as folder:
    out = Path(folder) / 'demo.html'
    render(records, labels, scores, positions, str(out))
    assert out.stat().st_size > 100_000
snapshot = import_data({'source': 'demo'})
snapshot['categories'] = ['Gardening', 'Music']
result = semantic_map(snapshot)
assert len(result['points']) == 60
assert len(result['stars']) == 2
assert all(np.isfinite(p['x']) and np.isfinite(p['y']) for p in result['points'])
assert all(-1.001 <= p['similarity'] <= 1.001 for p in result['stars'])
print('Semantic smoke passed: synthetic clusters, embedded HTML, offline notes and assigned labels.')
