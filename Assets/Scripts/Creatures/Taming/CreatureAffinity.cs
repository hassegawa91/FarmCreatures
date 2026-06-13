using UnityEngine;

namespace FarmCreatures.Creatures.Taming
{
    public class CreatureAffinity : MonoBehaviour
    {
        [Header("Taming")]
        [SerializeField] private CreatureTamingState state = CreatureTamingState.Wild;
        [SerializeField] private int affinity = 0;
        [SerializeField] private int tameThreshold = 100;

        public CreatureTamingState State => state;
        public int Affinity => affinity;

        public void AddAffinity(int amount)
        {
            affinity += amount;
            affinity = Mathf.Clamp(affinity, 0, tameThreshold);

            UpdateState();

            Debug.Log($"{name} afinidade: {affinity}/{tameThreshold} - Estado: {state}");
        }

        private void UpdateState()
        {
            if (affinity >= tameThreshold)
            {
                state = CreatureTamingState.Tamed;
                return;
            }

            if (affinity >= tameThreshold * 0.6f)
            {
                state = CreatureTamingState.Friendly;
                return;
            }

            if (affinity >= tameThreshold * 0.25f)
            {
                state = CreatureTamingState.Curious;
                return;
            }

            state = CreatureTamingState.Wild;
        }

        public void SetCompanion()
        {
            if (state == CreatureTamingState.Tamed)
                state = CreatureTamingState.Companion;
        }

        public void Store()
        {
            if (state == CreatureTamingState.Tamed || state == CreatureTamingState.Companion)
                state = CreatureTamingState.Stored;
        }
    }
}
