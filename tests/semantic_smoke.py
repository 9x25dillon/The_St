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
assert all(np.isfinite(p['iws']) and 0 <= p['iws'] <= 1.001 for p in result['points'])
assert all(isinstance(p['signal'], bool) for p in result['points'])
islands = result['islands']
assert sum(i['size'] for i in islands) + result['unclustered'] == 60
assert all(result['points'][c]['label'] == i['label'] for i in islands for c in i['central'])
assert [i['size'] for i in islands] == sorted((i['size'] for i in islands), reverse=True)
assert any('piano' in i['terms'] for i in islands), 'Distinctive words missing from the music island'
assert all('reflection' not in i['terms'][:3] for i in islands), 'A word every island shares headlined a card'
assert {label for i in islands for label in i['labels']} <= {'Gardening', 'Music'}
assert all(set(p['factors']) == {'fit', 'clarity', 'recency', 'typical'} for p in result['points'])
assert all(0 <= v <= 1.001 for p in result['points'] for v in p['factors'].values())
assert all(i['activity'] is None and i['trend'] is None for i in islands)  # the sample journal is undated
assert result['activity_axis'] is None
health = result['health']
assert 0 <= health['snr'] <= 1.001
assert 0 <= health['homogenization'] <= 1.001
assert 0 <= health['profile_health'] <= 1.001
assert sum(count for _, count in health['held_back_by']) == sum(not p['signal'] for p in result['points'])
print('Semantic smoke passed: synthetic clusters, embedded HTML, offline notes and assigned labels, island summaries, score factors, profile health.')
