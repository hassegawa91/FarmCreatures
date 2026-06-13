using FarmCreatures.World.Interaction;
using UnityEngine;

namespace FarmCreatures.Creatures.Taming
{
    [RequireComponent(typeof(CreatureAffinity))]
    public class CreatureTamingInteractable : MonoBehaviour, IInteractable
    {
        [SerializeField] private string interactionLabel = "Acariciar";
        [SerializeField] private int affinityPerInteraction = 10;

        private CreatureAffinity affinity;

        public string InteractionLabel => interactionLabel;

        private void Awake()
        {
            affinity = GetComponent<CreatureAffinity>();
        }

        public void Interact(InteractionContext context)
        {
            affinity.AddAffinity(affinityPerInteraction);
        }
    }
}
