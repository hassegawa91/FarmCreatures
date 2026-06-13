using UnityEngine;

namespace FarmCreatures.Farm
{
    public enum FarmTileState
    {
        Empty,
        Prepared,
        Planted,
        Watered,
        Blocked
    }

    public class FarmTile : MonoBehaviour
    {
        [SerializeField] private FarmTileState state = FarmTileState.Empty;

        public FarmTileState State => state;

        public void Prepare()
        {
            if (state == FarmTileState.Empty)
                state = FarmTileState.Prepared;
        }

        public void Plant()
        {
            if (state == FarmTileState.Prepared)
                state = FarmTileState.Planted;
        }

        public void Water()
        {
            if (state == FarmTileState.Planted)
                state = FarmTileState.Watered;
        }

        public void Clear()
        {
            state = FarmTileState.Empty;
        }
    }
}
